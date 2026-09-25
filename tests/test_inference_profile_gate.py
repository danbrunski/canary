"""R03.12 — IF and the sequence detector score only under a corpus-eligible
capture profile (operator decision 2026-09-24: tuning logs are not in the
anomaly detectors).

The vehicle-state gate alone let IF/sequence score a whole ``tuning`` capture:
the Xterra ``tuning`` profile polls Mode 01 RPM, so the state reads
``engine_running``. These tests pin the second gate beside it, its own
counter (``skipped_capture_profile``), its ``/status`` surface, and that a
replay is judged by the replayed session's manifest, not the live profile.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.collection import tuning_profile
from src.collection.capture_profiles import CaptureProfile
from src.config import CanaryConfig
from src.control.replay_control import ReplayController, ReplayNotFoundError, ReplayStatus
from src.daemon import build_components, build_live_feature_source, run_live_inference
from src.daemon.inference import (
    SequenceDetector,
    TickContext,
    capture_profile_gate,
)
from src.dashboard.status import build_status
from src.models.manager import LoadedModel
from src.models.manifest import ModelManifestCommon
from src.telemetry import TelemetryRecord
from tests.test_daemon import _config_yaml
from tests.waits import settle, wait_until

#: The Xterra ``tuning`` shape that exposed the bug: Mode 01 RPM polled, not
#: corpus-eligible (so its sessions land under ``_capture-tuning-*``).
_TUNING = CaptureProfile(
    name="tuning",
    description="R03.12 test profile",
    mode01={"RPM": 1.0},
    corpus_eligible=False,
    storage_prefix="_capture-tuning",
)


@pytest.fixture
def config(tmp_path: Path) -> CanaryConfig:
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_config_yaml(tmp_path))
    return CanaryConfig.from_yaml(cfg_path)


@pytest.fixture(autouse=True)
def _default_profile() -> Iterator[None]:
    """The active profile is process state; never leak it between tests."""
    tuning_profile.set_active(False, source="test")
    yield
    tuning_profile.set_active(False, source="test")


class _AlwaysFiresIF:
    """An IF artifact whose every tick is a calibrated anomaly."""

    feature_columns = ("obd:RPM",)
    mode_column = "operating_mode"
    manifest = type("M", (), {"model_version": "fake_if_v1"})()

    def score(self, frame: Any) -> Any:
        out = frame.copy()
        out["isolation_forest_score"] = 0.9
        out["isolation_forest_label"] = -1
        out["isolation_forest_model_mode"] = "idle"
        out["isolation_forest_missing_features"] = False
        out["isolation_forest_calibrated_threshold"] = 0.5
        out["isolation_forest_calibrated_anomaly"] = True
        return out


def _loaded(model_type: str, artifact: Any) -> LoadedModel:
    return LoadedModel(
        artifact=artifact,
        manifest=ModelManifestCommon(
            artifact_format_version=1,
            model_type=model_type,
            model_version=f"fake_{model_type}_v1",
            created_at="now",
            training_data_hash="h",
            input_rows=1,
        ),
        artifact_path=Path("/nonexistent/x"),
        manifest_path=Path("/nonexistent/x.manifest.json"),
        manifest_mtime=0.0,
    )


class _StubManager:
    def __init__(self, loaded: LoadedModel) -> None:
        self._loaded = loaded

    @property
    def current(self) -> LoadedModel:
        return self._loaded

    def poll(self) -> None:
        return None


def _components_with_if(config: CanaryConfig) -> Any:
    components = build_components(config)
    components.model_managers["isolation_forest"] = _StubManager(  # type: ignore[assignment]
        _loaded("isolation_forest", _AlwaysFiresIF())
    )
    return components


def _running_source(components: Any) -> Any:
    source = build_live_feature_source(components)
    source.observe(TelemetryRecord.obd(datetime.now(UTC).timestamp(), "RPM", 2500.0, "rpm"))
    return source


async def _feed_rpm(source: Any, stop: asyncio.Event) -> None:
    """Keep OBD fresh so the vehicle state stays ``engine_running``."""
    while not stop.is_set():
        source.observe(TelemetryRecord.obd(datetime.now(UTC).timestamp(), "RPM", 2500.0, "rpm"))
        await asyncio.sleep(0.02)


async def test_non_eligible_profile_withholds_if_while_engine_running(
    config: CanaryConfig,
) -> None:
    components = _components_with_if(config)
    tuning_profile.activate(_TUNING, source="test")
    source = _running_source(components)
    sub = components.anomalies_broadcaster.subscribe()
    stop = asyncio.Event()
    feeder = asyncio.create_task(_feed_rpm(source, stop))
    task = asyncio.create_task(run_live_inference(source, components, stop, tick_seconds=0.05))
    try:
        await wait_until(
            lambda: components.emitter.counters.skipped_capture_profile >= 3,
            what="ticks withheld on the capture profile",
        )
    finally:
        stop.set()
        await settle(task)
        await settle(feeder)

    assert sub.queue.empty(), "IF must not emit while a non-default profile polls"
    # The engine was running: the vehicle-state gate did not withhold it.
    assert components.emitter.counters.skipped_vehicle_state == 0
    assert components.emitter.counters.emitted == 0

    status = build_status(
        config, emitter=components.emitter, health_monitor=components.health_monitor
    )
    assert status.detectors is not None
    assert status.detectors["capture_profile_gate"] == {
        "scoring_allowed": False,
        "profile": "tuning",
        "source": "live_profile",
    }
    assert status.emitter_counters is not None
    assert status.emitter_counters["skipped_capture_profile"] >= 3


async def test_scoring_resumes_on_return_to_default(config: CanaryConfig) -> None:
    components = _components_with_if(config)
    tuning_profile.activate(_TUNING, source="test")
    source = _running_source(components)
    sub = components.anomalies_broadcaster.subscribe()
    stop = asyncio.Event()
    feeder = asyncio.create_task(_feed_rpm(source, stop))
    task = asyncio.create_task(run_live_inference(source, components, stop, tick_seconds=0.05))
    try:
        await wait_until(
            lambda: components.emitter.counters.skipped_capture_profile > 0,
            what="a tick withheld on the capture profile",
        )
        assert sub.queue.empty()
        tuning_profile.set_active(False, source="test")
        msg = await settle(sub.get())
    finally:
        stop.set()
        await settle(task)
        await settle(feeder)

    assert msg["source"] == "isolation_forest"
    gate = components.health_monitor.capture_profile_gate
    assert gate is not None and gate["scoring_allowed"] is True
    assert gate["profile"] == "default"


async def test_replay_of_a_non_eligible_capture_scores_nothing(config: CanaryConfig) -> None:
    """During a replay the gate reads the replayed session's manifest — here a
    tuning capture — even though the daemon's own profile is ``default``."""
    components = _components_with_if(config)

    class _StubReplayController:
        def status(self) -> ReplayStatus:
            return ReplayStatus(
                running=True,
                replay_id="replay-1",
                source_capture_profile="tuning",
                source_corpus_eligible=False,
            )

    components.replay_controller = _StubReplayController()  # type: ignore[assignment]
    assert tuning_profile.active_profile().corpus_eligible is True
    source = _running_source(components)
    sub = components.anomalies_broadcaster.subscribe()
    stop = asyncio.Event()
    task = asyncio.create_task(run_live_inference(source, components, stop, tick_seconds=0.05))
    try:
        await wait_until(
            lambda: components.emitter.counters.skipped_capture_profile >= 2,
            what="replay ticks withheld on the capture manifest",
        )
    finally:
        stop.set()
        await settle(task)

    assert sub.queue.empty()
    assert components.health_monitor.capture_profile_gate == {
        "scoring_allowed": False,
        "profile": "tuning",
        "source": "replay_manifest",
    }


def test_replay_of_a_default_session_scores_under_a_live_tuning_profile(
    config: CanaryConfig,
) -> None:
    """The replay manifest wins in both directions."""
    components = build_components(config)

    class _StubReplayController:
        def status(self) -> ReplayStatus:
            return ReplayStatus(running=True, replay_id="replay-1")

    components.replay_controller = _StubReplayController()  # type: ignore[assignment]
    tuning_profile.activate(_TUNING, source="test")
    gate = capture_profile_gate(components)
    assert gate.scoring_allowed is True
    assert gate.source == "replay_manifest"


def test_sequence_detector_prepares_nothing_when_scoring_is_withheld(
    config: CanaryConfig,
) -> None:
    """The sequence detector reads the same ``TickContext.scoring_allowed``
    the profile gate clears; it must not build a job from it."""
    components = build_components(config)
    components.sequence_model_type = "lstm_autoencoder"
    components.model_managers["lstm_autoencoder"] = _StubManager(  # type: ignore[assignment]
        _loaded("lstm_autoencoder", object())
    )
    detector = SequenceDetector(components)
    ctx = TickContext(
        now=datetime.now(UTC), operating_mode="idle", timestamp_str=None, scoring_allowed=False
    )
    assert detector.prepare(object(), ctx) is None


def _replay_controller(tmp_path: Path) -> tuple[ReplayController, Path]:
    from src.alerts.event_logger import AnomalyEventLogger
    from src.storage.parquet_writer import ParquetTelemetryWriter

    storage_root = tmp_path / "sessions"
    storage_root.mkdir()
    controller = ReplayController(
        queue=asyncio.Queue(),
        writer=ParquetTelemetryWriter(storage_root),
        anomaly_logger=AnomalyEventLogger(tmp_path / "anomalies"),
        storage_root=storage_root,
    )
    return controller, storage_root


def _write_manifest(session_dir: Path, *, profile: str, corpus_eligible: bool) -> None:
    session_dir.mkdir(parents=True)
    (session_dir / "capture_profile.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "profile": profile,
                "started_at": "2026-09-24T12:00:00+00:00",
                "profile_snapshot": {"name": profile, "corpus_eligible": corpus_eligible},
            }
        )
    )


async def test_replay_controller_refuses_capture_directories(tmp_path: Path) -> None:
    """First line of defence: a ``_capture-tuning-*`` directory is not a live
    session name, so it cannot be replayed at all."""
    controller, storage_root = _replay_controller(tmp_path)
    name = "_capture-tuning-2026-09-24T120000"
    _write_manifest(storage_root / name, profile="tuning", corpus_eligible=False)
    with pytest.raises(ReplayNotFoundError):
        await controller.start(name)


async def test_replay_controller_reads_the_source_manifest(tmp_path: Path) -> None:
    """Second line: a live-named session that carries a non-eligible manifest
    replays with ``source_corpus_eligible=False``; one without a manifest is
    default-profile collection and stays eligible."""
    controller, storage_root = _replay_controller(tmp_path)
    _write_manifest(storage_root / "2026-09-24T120000", profile="tuning", corpus_eligible=False)
    status = await controller.start("2026-09-24T120000", loop=False)
    try:
        assert status.source_corpus_eligible is False
        assert status.source_capture_profile == "tuning"
        # Not part of the /control/replay payload: the gate reads it, the
        # response shape is unchanged.
        assert "source_corpus_eligible" not in status.to_dict()
    finally:
        await controller.stop()

    (storage_root / "2026-09-24T130000").mkdir()
    status = await controller.start("2026-09-24T130000", loop=False)
    try:
        assert status.source_corpus_eligible is True
        assert status.source_capture_profile is None
    finally:
        await controller.stop()


# ---------------------------------------------------------------------------
# R03.1 — the inference gate's vehicle state gained the broadcast fallback;
# the capture-profile gate is what keeps an all-DID capture unscored now.
# ---------------------------------------------------------------------------

#: 0x1F9 bytes 2-3 big-endian, 8 counts/rpm: 16000 -> 2000 rpm.
_RPM_2000_FRAME = bytes([0x00, 0x00, 0x3E, 0x80, 0x00, 0x00, 0x00, 0x00])

_TUNING_DID = CaptureProfile(
    name="tuning-did",
    description="R03.1 all-DID capture: Mode 01 dark by the ECU's design",
    mode01={},
    mode22=("ECM_RPM_DID",),
    did_continuous=True,
    corpus_eligible=False,
    storage_prefix="_capture-tuning-did",
)


async def _feed_broadcast(source: Any, stop: asyncio.Event) -> None:
    while not stop.is_set():
        source.observe(TelemetryRecord.can(datetime.now(UTC).timestamp(), 0x1F9, _RPM_2000_FRAME))
        await asyncio.sleep(0.02)


def test_inference_gate_reads_engine_running_from_the_broadcast(config: CanaryConfig) -> None:
    """Mode 01 stale (40 s old), broadcast rpm fresh: the inference gate's own
    derivation must agree with ``/status`` (it used to say ``engine_off``)."""
    from datetime import timedelta

    from src.daemon.inference import _live_vehicle_state
    from src.dashboard.status import gather_status_sources

    components = build_components(config)
    source = build_live_feature_source(components)
    now = datetime.now(UTC)
    source.observe(
        TelemetryRecord.obd((now - timedelta(seconds=40)).timestamp(), "RPM", 750.0, "rpm")
    )
    source.observe(TelemetryRecord.can(now.timestamp(), 0x1F9, _RPM_2000_FRAME))

    gate_state = _live_vehicle_state(source, components, now)
    status_state = gather_status_sources(config, live_source=source).vehicle_state

    assert gate_state.state == "engine_running"
    assert gate_state.rpm == 2000.0
    assert status_state.state == gate_state.state


async def test_all_did_capture_is_withheld_by_the_profile_not_the_state(
    config: CanaryConfig,
) -> None:
    components = _components_with_if(config)
    tuning_profile.activate(_TUNING_DID, source="test")
    source = build_live_feature_source(components)
    sub = components.anomalies_broadcaster.subscribe()
    stop = asyncio.Event()
    feeder = asyncio.create_task(_feed_broadcast(source, stop))
    task = asyncio.create_task(run_live_inference(source, components, stop, tick_seconds=0.05))
    try:
        await wait_until(
            lambda: components.emitter.counters.skipped_capture_profile >= 3,
            what="all-DID ticks withheld on the capture profile",
        )
    finally:
        stop.set()
        await settle(task)
        await settle(feeder)

    assert sub.queue.empty()
    # Before R03.1 these ticks were withheld as `engine_off`: right outcome,
    # wrong reason. Now the state is right and the profile withholds them.
    assert components.emitter.counters.skipped_vehicle_state == 0
