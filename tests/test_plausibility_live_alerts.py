"""A05.9: a confirmed plausibility detection reaches the daemon's emitter.

``PlausibilityEvaluator(alert_sink=)`` and ``AnomalyEventEmitter.
emit_plausibility`` exist; this pins the daemon's own wiring
(``LivePlausibility.build``) with real ``build_components``: a confirmed
detection is an anomaly event and a push, a pending one is evidence only, and
in replay mode the event is still stored but nothing is pushed. Plausibility
is not capture-profile gated (R03.12c): it runs under a tuning capture too.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from src.alerts.event_logger import read_events_dir
from src.alerts.notifier import Notifier
from src.collection import tuning_profile
from src.collection.capture_profiles import CaptureProfile
from src.config import CanaryConfig
from src.daemon import build_components
from src.daemon.inference import _build_plausibility
from src.daemon.inference_metrics import InferenceTimingRecorder
from src.features.live import LiveFeatureSource
from src.plausibility.limits import PLAUSIBILITY_FILENAME
from src.telemetry import TelemetryRecord
from tests.configs import isolated_config

REPO = Path(__file__).resolve().parents[1]
XTERRA_PROFILE = REPO / "data" / "nissan-xterra-2008" / PLAUSIBILITY_FILENAME


class _RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def notify(self, title: str, message: str, *, priority: str = "default") -> None:
        self.calls.append((title, message))


def _config(tmp_path: Path) -> CanaryConfig:
    vehicle = tmp_path / "vehicle"
    vehicle.mkdir()
    (vehicle / PLAUSIBILITY_FILENAME).write_text(XTERRA_PROFILE.read_text())
    return isolated_config(
        tmp_path,
        "example",
        daemon={"afr_calibration": vehicle / "afr_calibration.yaml"},
        plausibility={"enabled": True},
        alerts={"min_seconds_between_events": 0},
        profiles={"enabled": False},
    )


def _confirm_overspeed(
    components: object, source: LiveFeatureSource, live: object, start: datetime
) -> None:
    """Two out-of-range SPEED samples 2 s apart: pending, then confirmed."""
    for i in range(2):
        at = start.replace(second=start.second + i * 2)
        source.observe(TelemetryRecord(at, "obd", "SPEED", 400.0, "kph"))
        live.tick_live(components, at, "city")  # type: ignore[attr-defined]


def test_a_confirmed_detection_reaches_the_emitter_and_replay_never_pushes(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    components = build_components(config)
    notifier = _RecordingNotifier()
    components.emitter._notifier = cast(Notifier, notifier)
    # R03.12(c): plausibility is not withheld under a tuning capture.
    tuning_profile.activate(
        CaptureProfile(
            name="tuning",
            mode01={"SPEED": 1.0},
            corpus_eligible=False,
            storage_prefix="_capture-tuning",
        ),
        source="test",
    )
    source = LiveFeatureSource(config=components.config)
    live = _build_plausibility(components, source, InferenceTimingRecorder())
    assert live is not None

    _confirm_overspeed(components, source, live, datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC))

    events = [e for e in read_events_dir(config.daemon.anomalies_dir) if e.source == "plausibility"]
    assert len(events) == 1
    assert events[0].affected_channels == ("obd:SPEED",)
    assert components.emitter.counters.emitted == 1
    assert len(notifier.calls) == 1

    # Replay mode (what ReplayController.start puts the emitter in): the event
    # is still emitted, the push is not.
    components.emitter.enter_replay_mode()
    _confirm_overspeed(components, source, live, datetime(2026, 9, 25, 12, 5, 0, tzinfo=UTC))
    assert components.emitter.counters.emitted == 1, "replay counters start clean"
    assert len(notifier.calls) == 1, "a replay never pushes"
