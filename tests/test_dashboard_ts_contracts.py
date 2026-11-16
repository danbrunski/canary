"""R28.2: the dashboard's hand-copied response types say what the daemon serves.

``fetchJson<T>`` is an unchecked cast and the backend declares no
``response_model``, so a field the daemon adds, renames or retypes leaves the
TypeScript quietly wrong: seven drifts were found by a spot check, among them a
detector-serving-the-wrong-manifest field the dashboard could not show. Codegen
stays declined (R08); this is the mechanism that replaces remembering, in the
shape of ``tests/test_brain_job_kinds_ts.py``.

Each case builds the REAL payload with the code that serves it, reads the
interface (or literal list) out of the ``.ts`` file with
``tests/ts_interfaces.py``, and compares them. A failure names the key and the
side it is missing from. A ``?`` member may be absent from a payload; a payload
key the type lacks, a required member no payload carries, and a value whose
kind the type rules out (``bool`` under ``number | null``) all fail.

Not covered (types and routes this file does not read): the ~390 other exported
types, ``CompactStatusResponse`` (``compact_status``), ``health.power`` (needs
``vcgencmd``), ``health.obd_scheduler`` and the inner shapes of
``capture_lifetime`` / ``telemetry_sinks``, ``detectors``, ``model_provenance``
and ``gps`` / ``imu`` / ``trip``. Widen one type at a time: add a case here.

``CANARY_UPDATE_CONTRACT_FIXTURES=1 pytest tests/test_dashboard_ts_contracts.py``
regenerates ``captureProfileDefaults.fixture.json`` (then ``pnpm exec prettier
--write src/test/captureProfileDefaults.fixture.json`` in
``src/dashboard/static``: ``pnpm format:check`` covers the file).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_args

import pytest
from fastapi.testclient import TestClient

from src.alerts.event_logger import AnomalyEventLogger
from src.alerts.events import AnomalyEvent, AnomalySource
from src.analysis import rom_editor
from src.cgroup_memory import memory_limit_status
from src.collection import bus_budget, capture_lifetime, tuning_profile
from src.collection.capture_profile_model import CaptureProfile
from src.control.replay_control import ReplayController
from src.control.sequence_detector import SequenceDetectorController
from src.daemon.detectors.common import _publish_score
from src.daemon.health import HealthMonitor, VersionPair
from src.daemon.host_metrics import system_metrics
from src.dashboard.api import create_app
from src.models.feedback import AnomalyFeedback, annotate_events_with_feedback, anomaly_key
from src.models.inference import SuppressionComparison
from src.models.role_gate import RoleResolution
from src.paths import REPO_ROOT
from src.startup import StartupTimeline
from src.storage.parquet_writer import ParquetTelemetryWriter
from tests.configs import isolated_config
from tests.ts_interfaces import (
    LIB,
    STATIC,
    TYPES,
    TsField,
    compare,
    const_number,
    const_strings,
    interface,
)

REPO = REPO_ROOT
LIVE = TYPES / "live.ts"
CONTROL = TYPES / "control.ts"
COVERAGE = TYPES / "coverage.ts"
DEFAULTS_FIXTURE = STATIC / "src" / "test" / "captureProfileDefaults.fixture.json"

#: What the editor's blank profile fills in itself rather than taking from the
#: model: the name is a timestamp slug and the prefix is derived from it (the
#: model refuses a non-corpus profile with no ``_capture-<slug>`` prefix).
BLANK_PROFILE_OWN_FIELDS = {"name", "storage_prefix"}


def _assert_agree(problems: list[str]) -> None:
    assert not problems, "\n".join(problems)


# ---------------------------------------------------------------------------
# AnomalyEvent, ScoreMessage and the source lists
# ---------------------------------------------------------------------------


def _event() -> AnomalyEvent:
    return AnomalyEvent(
        timestamp="2026-11-16T00:00:00Z", source="fused", model_version="v1", score=1.0
    )


class _OneVerdict:
    """The part of ``FeedbackStore`` ``annotate_events_with_feedback`` reads."""

    def __init__(self, event: dict[str, Any]) -> None:
        self._labels = {
            anomaly_key(event): AnomalyFeedback(
                anomaly_key=anomaly_key(event), label="true_positive", event=event
            )
        }

    def labels(self) -> dict[str, AnomalyFeedback]:
        return self._labels


def test_the_dashboard_lists_every_anomaly_source_in_order() -> None:
    assert const_strings(LIVE, "ANOMALY_SOURCES") == list(get_args(AnomalySource))


def test_the_event_type_uses_the_shared_source_list() -> None:
    event = interface(LIVE, "AnomalyEvent")
    score = interface(LIVE, "ScoreMessage")
    assert event["source"].type == "AnomalySource"
    assert score["source"].type == "ScoreSource"


def test_the_score_sources_are_the_ones_the_daemon_publishes() -> None:
    published: set[str] = set()
    for path in (REPO / "src" / "daemon").rglob("*.py"):
        text = path.read_text()
        for call in re.finditer(r"\b_publish_score\(([^)]*)\)", text, re.DOTALL):
            published.update(re.findall(r'\bsource="(\w+)"', call.group(1)))
    assert published, "found no _publish_score call: the search is stale"
    ts_sources = const_strings(LIVE, "SCORE_SOURCES")
    assert set(ts_sources) == published
    assert set(ts_sources) <= set(get_args(AnomalySource))


def test_the_anomaly_event_type_matches_the_payload() -> None:
    plain = _event().to_dict()
    # `GET /anomalies` joins the operator's verdict on; a `/ws` frame carries none.
    joined = annotate_events_with_feedback([plain], _OneVerdict(plain))[0]  # type: ignore[arg-type]
    _assert_agree(compare("AnomalyEvent", [plain, joined], interface(LIVE, "AnomalyEvent")))


def test_the_feedback_type_matches_the_payload() -> None:
    payload = AnomalyFeedback(anomaly_key="k", label="ignored").to_dict()
    _assert_agree(compare("FeedbackRecord", [payload], interface(COVERAGE, "FeedbackRecord")))


def test_the_score_message_type_matches_the_payload() -> None:
    class _Sink:
        def __init__(self) -> None:
            self.frames: list[dict[str, Any]] = []

        def publish(self, message: dict[str, Any]) -> None:
            self.frames.append(message)

    sink = _Sink()
    _publish_score(
        sink,  # type: ignore[arg-type]
        source="fused",
        score=1.0,
        threshold=None,
        operating_mode=None,
        model_version="v1",
        timestamp="2026-11-16T00:00:00Z",
        missing_features=False,
    )
    _assert_agree(compare("ScoreMessage", sink.frames, interface(LIVE, "ScoreMessage")))


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------


def _status_route_body(tmp_path: Path) -> dict[str, Any]:
    """The body of a real ``GET /status`` with every optional block wired.

    A health monitor with each detector block set and a capture active, and the
    real replay and sequence-detector controllers, so ``replay`` and
    ``sequence_detector`` (which only the route adds) are in the body. Reading
    the route, not its source, means a key added by any means is compared.
    """
    storage = tmp_path / "storage"
    storage.mkdir()
    config = isolated_config(
        tmp_path,
        "daemon",
        storage=storage,
        models={"isolation_forest": {"enabled": True}, "lstm_autoencoder": {"enabled": True}},
        dashboard={"port": 8000},
        profiles={"enabled": False},
    )
    replay = ReplayController(
        queue=asyncio.Queue(),
        writer=ParquetTelemetryWriter(storage),
        anomaly_logger=AnomalyEventLogger(tmp_path / "anomalies"),
        storage_root=storage,
        broadcast_stale_timeout_seconds=10.0,
    )
    sequence_components = SimpleNamespace(
        config=config,
        model_managers={"isolation_forest": SimpleNamespace(current=None)},
        sequence_model_type="lstm_autoencoder",
    )
    monitor = _full_monitor(tmp_path)
    app = create_app(
        config,
        anomalies_dir=tmp_path / "anomalies",
        health_monitor=monitor,
        replay_controller=replay,
        sequence_detector_controller=SequenceDetectorController(sequence_components),
    )
    capture_lifetime._reset_for_tests()
    try:
        capture_lifetime.begin(0.0, profile="fuel-map", generation=2)
        response = TestClient(app).get("/status")
    finally:
        capture_lifetime._reset_for_tests()
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def test_the_status_response_type_matches_the_route(tmp_path: Path) -> None:
    body = _status_route_body(tmp_path)
    # The route-added blocks really are in the body, so they are compared too.
    for key in ("replay", "sequence_detector", "accepted_baseline_overrides", "network"):
        assert key in body, key
    _assert_agree(compare("StatusResponse", [body], interface(LIVE, "StatusResponse")))


def test_the_health_type_matches_the_route(tmp_path: Path) -> None:
    health = _status_route_body(tmp_path)["health"]
    assert "capture_lifetime" in health and "model_version_mismatches" in health
    _assert_agree(compare("StatusHealth", [health], interface(LIVE, "StatusHealth")))


def test_the_capture_lifetime_type_matches_the_payload(tmp_path: Path) -> None:
    lifetime = _status_route_body(tmp_path)["health"]["capture_lifetime"]
    _assert_agree(
        compare("CaptureLifetimeStatus", [lifetime], interface(LIVE, "CaptureLifetimeStatus"))
    )


def test_the_startup_type_matches_the_payload() -> None:
    _assert_agree(
        compare("StatusStartup", [StartupTimeline().snapshot()], interface(LIVE, "StatusStartup"))
    )


def test_the_bus_budget_type_matches_the_payload() -> None:
    _assert_agree(
        compare("BusBudgetStatus", [bus_budget.snapshot()], interface(LIVE, "BusBudgetStatus"))
    )


def test_the_active_capture_type_matches_the_payload() -> None:
    """``/status.capture_profile`` and ``tuning_mode``: ``tuning_profile.snapshot()``,
    with ``achieved_hz`` when the caller has it and ``rows_written`` from the status builder."""
    payload = {**tuning_profile.snapshot(achieved_hz={}), "rows_written": 0}
    _assert_agree(
        compare("CaptureProfileState", [payload], interface(CONTROL, "CaptureProfileState"))
    )


def _full_monitor(tmp_path: Path) -> HealthMonitor:
    """A monitor with every detector block set, a sink reporting and can0 counters."""
    stats = tmp_path / "statistics"
    stats.mkdir(exist_ok=True)
    for counter in ("rx_over_errors", "rx_dropped", "rx_errors"):
        (stats / counter).write_text("0")

    class _Queue:
        maxsize = 8

        def qsize(self) -> int:
            return 0

    full_monitor = HealthMonitor(can0_stats_path=stats)
    full_monitor.set_detector_block(
        "model_version_mismatch", VersionPair(serving="a", manifest="b"), key="isolation_forest"
    )
    full_monitor.set_detector_block(
        "sequence_suppression",
        SuppressionComparison(
            model_version="v1",
            training=("a",),
            serving=("a",),
            trained_columns_suppressed_at_serve=(),
        ).to_dict(),
    )
    full_monitor.set_detector_block("sequence_cadence_refusal", "cadence differs")
    full_monitor.set_detector_block(
        "detector_roles",
        {
            "isolation_forest": RoleResolution(
                source="isolation_forest", configured="alert", effective="evidence", reason="x"
            ).to_dict()
        },
    )
    full_monitor.set_detector_block("obd_scheduler", {})
    full_monitor.observe_telemetry_queue("storage", _Queue())
    return full_monitor


def _health_snapshots(tmp_path: Path) -> list[dict[str, Any]]:
    bare = HealthMonitor(can0_stats_path=tmp_path / "no-can0").snapshot(now_epoch=0.0)
    full_monitor = _full_monitor(tmp_path)
    capture_lifetime._reset_for_tests()
    try:
        capture_lifetime.begin(0.0, profile="fuel-map", generation=2)
        full = full_monitor.snapshot(
            now_epoch=0.0,
            scaler={"columns": ["a"], "means": {"a": 0.0}, "stds": {"a": 1.0}},
            live_means={"a": 10.0},
            lstm_thresholds={"global": {"mean": 0.1, "std": 0.05}},
            live_mean_recon_error=0.3,
        )
    finally:
        capture_lifetime._reset_for_tests()
    return [bare, full]


def test_the_health_type_matches_the_payload(tmp_path: Path) -> None:
    snapshots = _health_snapshots(tmp_path)
    # The full snapshot really carries every block the table named.
    for key in (
        "model_version_mismatches",
        "sequence_suppression",
        "sequence_cadence_refusal",
        "detector_roles",
        "capture_lifetime",
        "telemetry_sinks",
        "can0",
    ):
        assert key in snapshots[1], key
    _assert_agree(compare("StatusHealth", snapshots, interface(LIVE, "StatusHealth")))


def test_the_mismatch_and_role_entries_match_their_payloads(tmp_path: Path) -> None:
    full = _health_snapshots(tmp_path)[1]
    _assert_agree(
        compare(
            "VersionMismatch",
            list(full["model_version_mismatches"].values()),
            interface(LIVE, "VersionMismatch"),
        )
    )
    _assert_agree(
        compare(
            "DetectorRoleStatus",
            list(full["detector_roles"].values()),
            interface(LIVE, "DetectorRoleStatus"),
        )
    )
    _assert_agree(
        compare(
            "SequenceSuppression",
            [full["sequence_suppression"]],
            interface(LIVE, "SequenceSuppression"),
        )
    )


def test_health_system_is_typed_member_by_member() -> None:
    """``health.system`` is not a number map: it carries a bool and a string
    (``memory_limits_enforced``, ``memory_max_source``)."""
    payload = {**system_metrics(), **memory_limit_status(), "cpu_percent": None}
    declared = interface(LIVE, "StatusHealth")["system"].nested()
    assert declared is not None, "StatusHealth.system must list its members"
    _assert_agree(compare("StatusHealth.system", [payload], declared))
    # The cgroup reading has three states; each one's value kinds are declared.
    states: list[dict[str, Any]] = [
        {
            "memory_limits_enforced": True,
            "memory_max_bytes": 6_000_000_000,
            "memory_max_source": "/x",
        },
        {"memory_limits_enforced": False, "memory_max_bytes": None, "memory_max_source": "/x"},
        {"memory_limits_enforced": None, "memory_max_bytes": None, "memory_max_source": None},
    ]
    _assert_agree(
        compare("StatusHealth.system", [{**payload, **state} for state in states], declared)
    )


# ---------------------------------------------------------------------------
# CaptureProfile and the editor's blank profile
# ---------------------------------------------------------------------------


def _defaults() -> dict[str, Any]:
    return CaptureProfile(name="blank", storage_prefix="_capture-blank").model_dump(mode="json")


def test_the_capture_profile_type_matches_the_payload() -> None:
    _assert_agree(compare("CaptureProfile", [_defaults()], interface(CONTROL, "CaptureProfile")))


def test_the_editors_blank_profile_is_the_models_defaults() -> None:
    """``blankProfile()`` (``CaptureProfileEditor.tsx``) must equal ``CaptureProfile``'s
    defaults: it said ``round_timestamps: true`` while the model's default is
    ``False``, so a profile saved without touching the box recorded round-stamped
    rows nobody asked for. The vitest ``CaptureProfileEditor.test.tsx`` compares
    ``blankProfile()`` to this fixture field by field."""
    defaults = {k: v for k, v in _defaults().items() if k not in BLANK_PROFILE_OWN_FIELDS}
    if os.environ.get("CANARY_UPDATE_CONTRACT_FIXTURES") == "1":
        DEFAULTS_FIXTURE.write_text(json.dumps(defaults, indent=2) + "\n")
    assert json.loads(DEFAULTS_FIXTURE.read_text()) == defaults, (
        "captureProfileDefaults.fixture.json is stale: regenerate with "
        "CANARY_UPDATE_CONTRACT_FIXTURES=1 (see the module docstring)"
    )


# ---------------------------------------------------------------------------
# Constants restated on both sides
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["MAX_GRID_POINTS", "MAX_DRAWS"])
def test_the_simulation_bounds_match_the_editor_module(name: str) -> None:
    assert const_number(LIB / "romSim.ts", name) == getattr(rom_editor, name)


def test_the_reader_notices_a_missing_member() -> None:
    """A test of the test: a payload key the type lacks fails and names both sides."""
    declared: dict[str, TsField] = {"a": TsField("a", False, "number")}
    problems = compare("T", [{"a": 1, "b": 2}], declared)
    assert problems == ["T: the payload carries ['b'], the TS type does not"]
    problems = compare("T", [{}], declared)
    assert problems and "requires ['a']" in problems[0]
    problems = compare("T", [{"a": True}], declared)
    assert problems and "boolean" in problems[0]
