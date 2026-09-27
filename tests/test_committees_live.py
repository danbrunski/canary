"""H04.2: sensor committees on the daemon's live tick.

Real ``build_components`` + a real ``LiveFeatureSource``: telemetry records
go in, the tick's operating point goes through ``LiveCommittees``, and a
sustained OBD↔GPS speed departure comes out as an anomaly event (``source:
committee``) in the log, in ``/status`` ``detectors.inference.committees``,
through the dashboard's ``/anomalies`` route and the MCP
``get_recent_anomalies`` tool. Committees are not capture-profile gated
(R03.12 reasoning in ``src.daemon.committees_live``): the test runs under a
tuning capture. A replay stores the event and never pushes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from src.alerts.event_logger import read_events_dir
from src.analysis.committees import COMMITTEES_FILENAME, SPEED_COMMITTEE
from src.collection import tuning_profile
from src.collection.capture_profiles import CaptureProfile
from src.config import CanaryConfig
from src.daemon import build_components
from src.daemon.committees_live import LiveCommittees, build_live_committees
from src.daemon.inference_metrics import InferenceTimingRecorder
from src.dashboard.api import create_app
from src.mcp_server.client import DaemonClient
from src.mcp_server.tools import anomalies as anomalies_tool
from src.preprocessing.live import LiveFeatureSource
from src.telemetry import TelemetryRecord
from tests.configs import isolated_config

REPO = Path(__file__).resolve().parents[1]
XTERRA_COMMITTEES = REPO / "data" / "nissan-xterra-2008" / COMMITTEES_FILENAME
RATIO = 0.9203
T0 = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)


class _RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def notify(self, title: str, message: str, *, priority: str = "default") -> None:
        self.calls.append((title, message))


def _config(
    tmp_path: Path,
    *,
    with_profile: bool = True,
    committee_role: str = "alert",
    enabled: bool = True,
) -> CanaryConfig:
    vehicle = tmp_path / "vehicle"
    vehicle.mkdir()
    if with_profile:
        (vehicle / COMMITTEES_FILENAME).write_text(XTERRA_COMMITTEES.read_text())
    return isolated_config(
        tmp_path,
        "example",
        daemon={"afr_calibration": vehicle / "afr_calibration.yaml"},
        alerts={"min_seconds_between_events": 0},
        models={"roles": {"committee": committee_role}},
        committees={"enabled": enabled},
        profiles={"enabled": False},
    )


def _drive(
    components: Any,
    source: LiveFeatureSource,
    live: LiveCommittees,
    start: datetime,
    seconds: int,
    *,
    ecu_factor: float,
) -> None:
    """``seconds`` of 90 km/h GPS cruise, ECU speed at ``ecu_factor`` x its relation."""
    for i in range(seconds):
        at = start + timedelta(seconds=i)
        source.observe(TelemetryRecord(at, "gps", "speed", 90.0, "km/h"))
        source.observe(TelemetryRecord(at, "gps", "num_satellites", 10, ""))
        source.observe(TelemetryRecord(at, "gps", "hdop", 0.9, ""))
        source.observe(TelemetryRecord(at, "obd", "SPEED", round(RATIO * 90.0 * ecu_factor), "kph"))
        source.observe(TelemetryRecord(at, "obd", "RPM", 2200.0, "rpm"))
        tick = at + timedelta(milliseconds=500)
        source.tick(tick)
        live.tick_live(components, source.latest_operating_point, tick, None)


def _tuning_capture() -> None:
    tuning_profile.activate(
        CaptureProfile(
            name="tuning",
            mode01={"SPEED": 1.0},
            corpus_eligible=False,
            storage_prefix="_capture-tuning",
        ),
        source="test",
    )


def test_a_sustained_speed_disagreement_is_an_event_status_and_mcp_payload(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    components = build_components(config)
    notifier = _RecordingNotifier()
    components.emitter._notifier = notifier  # type: ignore[attr-defined]
    _tuning_capture()  # R03.12: committees keep running under a tuning capture
    recorder = InferenceTimingRecorder()
    source = LiveFeatureSource(config=components.config)
    live = build_live_committees(components, source, recorder)
    assert live is not None

    _drive(components, source, live, T0, 20, ecu_factor=1.0)
    assert read_events_dir(config.daemon.anomalies_dir) == []
    block = recorder.snapshot().to_dict()["committees"]
    speed = block["committees"][SPEED_COMMITTEE.name]
    assert speed["state"] == "agree" and speed["configured"] is True
    assert block["capture_profile_gated"] is False

    _drive(components, source, live, T0 + timedelta(seconds=20), 20, ecu_factor=0.75)
    events = [e for e in read_events_dir(config.daemon.anomalies_dir) if e.source == "committee"]
    assert len(events) == 1, "one event per episode"
    event = events[0]
    assert event.detail["detector"] == f"committee:{SPEED_COMMITTEE.name}"
    assert event.affected_channels == ("obd:SPEED", "gps:speed")
    assert event.detail["relation"]["ratio"] == RATIO
    assert len(notifier.calls) == 1 and "SPEED_SOURCE_DIVERGENCE" in notifier.calls[0][1]
    speed = recorder.snapshot().to_dict()["committees"]["committees"][SPEED_COMMITTEE.name]
    assert speed["state"] == "disagree" and speed["alerting"] is True and speed["events"] == 1

    # Through the dashboard route and the MCP tool, unchanged.
    app = create_app(config, anomalies_dir=config.daemon.anomalies_dir)
    payload = _mcp_recent_anomalies(app)
    assert [e["source"] for e in payload["events"]] == ["committee"]
    assert payload["events"][0]["detail"]["member"] == "obd:SPEED"

    # Replay: stored in the replay's log, never pushed.
    components.emitter.enter_replay_mode()
    _drive(components, source, live, T0 + timedelta(minutes=5), 3, ecu_factor=1.0)
    _drive(components, source, live, T0 + timedelta(minutes=5, seconds=3), 15, ecu_factor=0.75)
    assert components.emitter.counters.emitted == 1, "the replay's own event"
    assert len(notifier.calls) == 1, "a replay never pushes"


def _mcp_recent_anomalies(app: Any) -> dict[str, Any]:
    import asyncio

    async def call() -> dict[str, Any]:
        client = DaemonClient("http://daemon")
        await client._client.aclose()
        client._client = httpx.AsyncClient(
            base_url="http://daemon", transport=httpx.ASGITransport(app=app)
        )
        try:
            return await anomalies_tool.get_recent_anomalies(client, source="committee")
        finally:
            await client.aclose()

    return asyncio.run(call())


def test_no_profile_file_runs_every_committee_not_configured(tmp_path: Path) -> None:
    config = _config(tmp_path, with_profile=False)
    components = build_components(config)
    recorder = InferenceTimingRecorder()
    source = LiveFeatureSource(config=components.config)
    live = build_live_committees(components, source, recorder)
    assert live is not None
    _drive(components, source, live, T0, 15, ecu_factor=0.5)
    assert read_events_dir(config.daemon.anomalies_dir) == []
    block = recorder.snapshot().to_dict()["committees"]
    assert block["profile_path"].endswith(COMMITTEES_FILENAME)
    assert {s["reason"] for s in block["committees"].values()} == {"not_configured"}


def test_a_malformed_profile_is_withheld_and_named(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = config.daemon.afr_calibration.parent / COMMITTEES_FILENAME
    path.write_text("schema_version: 1\ncommittees: {NOPE: {}}\n")
    components = build_components(config)
    recorder = InferenceTimingRecorder()
    live = build_live_committees(components, LiveFeatureSource(config=components.config), recorder)
    assert live is None
    assert "committees" in recorder.snapshot().to_dict()["committees"]["withheld"]


def test_the_inference_pipeline_builds_and_ticks_committees(tmp_path: Path) -> None:
    """The pipeline owns one ``LiveCommittees`` and times it as a stage."""
    from src.daemon.inference import LiveInferencePipeline

    config = _config(tmp_path)
    components = build_components(config)
    recorder = InferenceTimingRecorder()
    source = LiveFeatureSource(config=components.config)
    pipeline = LiveInferencePipeline(source, components, interval_seconds=1.0, recorder=recorder)
    try:
        assert isinstance(pipeline._committees, LiveCommittees)
    finally:
        import asyncio

        asyncio.run(pipeline.close(timeout_seconds=1.0))


def test_committee_events_default_to_the_evidence_role(tmp_path: Path) -> None:
    """Until a committee has a confirmed real catch it stores, never pushes."""
    assert CanaryConfig.model_fields["committees"].default.enabled is True
    config = isolated_config(tmp_path, "example")
    assert config.models.roles.committee == "evidence"
    assert "committee" not in config.models.roles.by_source(), "not an artifact-gated source"
    config = _config(tmp_path, committee_role="evidence")
    components = build_components(config)
    notifier = _RecordingNotifier()
    components.emitter._notifier = notifier  # type: ignore[attr-defined]
    recorder = InferenceTimingRecorder()
    source = LiveFeatureSource(config=components.config)
    live = build_live_committees(components, source, recorder)
    assert live is not None
    _drive(components, source, live, T0, 3, ecu_factor=1.0)
    _drive(components, source, live, T0 + timedelta(seconds=3), 15, ecu_factor=0.75)
    events = [e for e in read_events_dir(config.daemon.anomalies_dir) if e.source == "committee"]
    assert [e.role for e in events] == ["evidence"]
    assert components.emitter.counters.emitted_evidence == 1
    assert notifier.calls == []


def test_committees_can_be_switched_off(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=False)
    components = build_components(config)
    recorder = InferenceTimingRecorder()
    source = LiveFeatureSource(config=components.config)
    assert build_live_committees(components, source, recorder) is None
    assert recorder.snapshot().to_dict()["committees"] == {"enabled": False}
