"""R03.5 — the profile-capture lifecycle without the web stack.

``ProfileCaptureSession`` owns what used to be seven ``nonlocal`` variables in
``register_control_routes``. These drive it directly, and pin the one
behaviour the move fixes: a capture ended by daemon shutdown is judged against
its own start epoch, so its ``loss_epoch_start`` is reported (the shutdown
path's own copy of the stop passed ``start=None``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

from src.collection import bus_budget, tuning_profile
from src.collection.capture_profiles import CaptureProfileStore
from src.config import CanaryConfig
from src.control import capture_finalize
from src.control.capture import CaptureController
from src.control.capture_manifest import read_capture_manifest
from src.control.profile_capture import (
    ProfileCaptureError,
    ProfileCaptureSession,
    ProfileCaptureStart,
)
from src.daemon.health import HealthMonitor
from src.storage.async_writer import AsyncParquetTelemetryWriter
from src.storage.parquet_writer import ParquetTelemetryWriter

EXAMPLE_YAML = Path(__file__).resolve().parents[1] / "canary.example.yaml"


@pytest.fixture(autouse=True)
def _process_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Profile and bus budget are process state; the finalizer's child is a
    thread here (its spawn path has its own tests)."""
    monkeypatch.setattr(capture_finalize, "_spawn_pool", lambda: ThreadPoolExecutor(1))
    tuning_profile.set_active(False, source="test")
    bus_budget.restore({"override_requests_per_second": None})
    yield
    tuning_profile.set_active(False, source="test")
    bus_budget.restore({"override_requests_per_second": None})


def _session(tmp_path: Path) -> tuple[ProfileCaptureSession, AsyncParquetTelemetryWriter]:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    store = CaptureProfileStore(
        tmp_path / "vehicle" / "capture_profiles.yaml",
        config.collection,
        tmp_path / "missing-behavior-models.yaml",
    )
    writer = AsyncParquetTelemetryWriter(
        ParquetTelemetryWriter(
            tmp_path / "sessions", session_started_at=datetime(2026, 9, 24, 12, tzinfo=UTC)
        )
    )
    stats = tmp_path / "can0-statistics"
    stats.mkdir()
    for name in ("rx_over_errors", "rx_dropped", "rx_errors"):
        (stats / name).write_text("0")
    health = HealthMonitor(can0_stats_path=stats)
    health.observe_obd_scheduler({"achieved_hz_capture": {}, "requests": [], "batching": {}})
    for name in ("ingest", "storage"):
        health.observe_telemetry_queue(name, asyncio.Queue())
    session = ProfileCaptureSession(
        storage_writer=writer,
        capture_controller=CaptureController(lambda: writer.paths.root),
        profile_store=store,
        health_monitor=health,
    )
    return session, writer


def _persistence(quality: dict[str, Any] | None) -> dict[str, Any]:
    assert quality is not None
    persistence = quality.get("persistence")
    assert isinstance(persistence, dict), json.dumps(quality)[:400]
    return persistence


async def test_start_then_stop_without_the_web_stack(tmp_path: Path) -> None:
    session, writer = _session(tmp_path)
    live_before = writer.paths.root

    started = await session.start(ProfileCaptureStart(profile="fuel-map"))
    capture_dir = writer.paths.root
    assert capture_dir.name.startswith("_capture-fuel-map-")
    assert started["output_session_dir"] == str(capture_dir)
    assert tuning_profile.active_profile().name == "fuel-map"
    assert session.is_writer_session(capture_dir.name)
    assert session.start_epoch is not None

    # Starting the same capture again is idempotent, a different one refused.
    again = await session.start(ProfileCaptureStart(profile="fuel-map"))
    assert again["idempotent"] is True

    stopped = await session.stop("operator_stop")
    assert stopped["ended_capture_dir"] == str(capture_dir)
    assert tuning_profile.active_profile().name == "default"
    assert not writer.paths.root.name.startswith("_")
    assert writer.paths.root != live_before
    manifest = read_capture_manifest(capture_dir)
    assert manifest is not None and manifest.end_reason == "operator_stop"
    assert _persistence(stopped["capture_quality"])["loss_epoch_start"] is not None
    assert session.start_epoch is None

    # A retried stop answers from the last one rather than 409-ing.
    retried = await session.stop("operator_stop")
    assert retried["idempotent"] is True


async def test_a_capture_ended_by_shutdown_reports_its_loss_epoch_start(
    tmp_path: Path,
) -> None:
    session, writer = _session(tmp_path)
    await session.start(ProfileCaptureStart(profile="fuel-map"))
    capture_dir = writer.paths.root

    stopped = await session.stop("daemon_shutdown")

    assert _persistence(stopped["capture_quality"])["loss_epoch_start"] is not None
    stored = json.loads((capture_dir / "capture_quality.json").read_text())
    assert _persistence(stored)["loss_epoch_start"] is not None
    manifest = read_capture_manifest(capture_dir)
    assert manifest is not None and manifest.end_reason == "daemon_shutdown"
    # As before: shutdown leaves the writer on the capture (the trip-end
    # fallback digests it) rather than rotating into an empty live session.
    assert writer.paths.root == capture_dir


async def test_refusals_carry_the_routes_status_codes(tmp_path: Path) -> None:
    session, _writer = _session(tmp_path)
    with pytest.raises(ProfileCaptureError) as unknown:
        await session.start(ProfileCaptureStart(profile="nope"))
    assert unknown.value.status == 404
    with pytest.raises(ProfileCaptureError) as default:
        await session.start(ProfileCaptureStart(profile="default"))
    assert default.value.status == 409
    with pytest.raises(ProfileCaptureError) as idle:
        await session.stop("operator_stop")
    assert idle.value.status == 409
    with pytest.raises(ProfileCaptureError) as paused:
        session.pause(True)
    assert paused.value.status == 409

    no_writer = ProfileCaptureSession(
        storage_writer=None,
        capture_controller=None,
        profile_store=session.profile_store,
        health_monitor=None,
    )
    with pytest.raises(ProfileCaptureError) as unavailable:
        await no_writer.start(ProfileCaptureStart(profile="fuel-map"))
    assert unavailable.value.status == 503


async def test_run_daemon_shutdown_stops_through_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_trip_end_digest_child: list[Path]
) -> None:
    """``run_daemon``'s shutdown calls ``stop("daemon_shutdown")`` on the same
    session the routes use, instead of its own finalize/stamp copy."""
    from src.collection.capture_profiles import CaptureProfile
    from src.daemon import build_components
    from src.daemon import runtime as runtime_mod
    from tests.configs import daemon_test_config
    from tests.waits import settle, wait_until

    # Every daemon state path under tmp_path (R07.2).
    config = daemon_test_config(tmp_path)
    reasons: list[str] = []

    class _Recording(ProfileCaptureSession):
        async def stop(self, reason: Any = "operator_stop") -> dict[str, Any]:
            reasons.append(reason)
            return {}

    monkeypatch.setattr(runtime_mod, "ProfileCaptureSession", _Recording)
    components = build_components(config)
    stop = asyncio.Event()
    task = asyncio.create_task(
        runtime_mod.run_daemon(
            config,
            stop,
            collectors=[],
            writer=ParquetTelemetryWriter(config.collection.storage_path),
            components=components,
            enable_live_inference=False,
        )
    )
    try:
        await wait_until(lambda: components.profile_capture is not None, what="session built")
        tuning_profile.activate(
            CaptureProfile(
                name="tuning",
                description="",
                mode01={"RPM": 1.0},
                corpus_eligible=False,
                storage_prefix="_capture-tuning",
            ),
            source="test",
        )
    finally:
        stop.set()
        await settle(task)

    assert reasons == ["daemon_shutdown"]


async def test_an_http_started_capture_ended_by_daemon_shutdown_reports_its_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_trip_end_digest_child: list[Path]
) -> None:
    """The daemon's app and its shutdown must share one session (R03.5).

    Before the fix, ``server._build_app`` passed no session, so the routes
    built a private one: a capture started over HTTP kept its start epoch
    there, while shutdown stopped ``components.profile_capture`` (no epoch)
    and the sidecar said ``loss_epoch_start: None``.
    """
    import httpx

    from src.daemon import build_components
    from src.daemon import runtime as runtime_mod
    from src.daemon import server as server_mod
    from tests.configs import daemon_test_config
    from tests.waits import settle, wait_until

    # Every daemon state path under tmp_path (R07.2): the full run_daemon
    # touches the audit log, trip odometer, catalog and more.
    config = daemon_test_config(
        tmp_path, daemon={"capture_profiles": tmp_path / "vehicle" / "capture_profiles.yaml"}
    )
    monkeypatch.setenv(config.daemon.control_token_env, "secret")

    apps: list[Any] = []
    real_build = server_mod._build_app

    def _capturing(components: Any) -> Any:
        app = real_build(components)
        apps.append(app)
        return app

    monkeypatch.setattr(server_mod, "_build_app", _capturing)
    components = build_components(config)
    stats = tmp_path / "can0-statistics"
    stats.mkdir()
    for name in ("rx_over_errors", "rx_dropped", "rx_errors"):
        (stats / name).write_text("0")
    components.health_monitor = HealthMonitor(can0_stats_path=stats)
    components.health_monitor.observe_obd_scheduler(
        {"achieved_hz_capture": {}, "requests": [], "batching": {}}
    )
    stop = asyncio.Event()
    task = asyncio.create_task(
        runtime_mod.run_daemon(
            config,
            stop,
            collectors=[],
            writer=ParquetTelemetryWriter(config.collection.storage_path),
            components=components,
            enable_live_inference=False,
        )
    )
    try:
        await wait_until(lambda: bool(apps), what="the daemon's app built")
        app = apps[0]
        assert app.state.control_context.session is components.profile_capture
        assert app.state.control_context.autostart is components.capture_autostart
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://daemon") as client:
            started = await client.post(
                "/control/capture",
                json={"profile": "fuel-map"},
                headers={"Authorization": "Bearer secret"},
            )
        assert started.status_code == 200, started.text
        capture_dir = Path(started.json()["output_session_dir"])
    finally:
        stop.set()
        await settle(task)

    stored = json.loads((capture_dir / "capture_quality.json").read_text())
    assert _persistence(stored)["loss_epoch_start"] is not None
    manifest = read_capture_manifest(capture_dir)
    assert manifest is not None and manifest.end_reason == "daemon_shutdown"


async def test_two_captures_started_in_one_second_keep_both_recordings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D03.32: the capture id was ``now`` to the second, so start, stop and
    start again inside one second re-entered the first capture's directory,
    and the second capture overwrote its ``telemetry-00000.parquet``."""
    from src import session as session_mod
    from src.control import profile_capture
    from src.telemetry import TelemetryRecord

    monkeypatch.setattr(session_mod, "_last_isolated_id", None)
    frozen = datetime(2026, 9, 25, 23, 30, 19, tzinfo=UTC)
    monkeypatch.setattr(
        profile_capture, "issue_isolated_id", lambda: session_mod.issue_isolated_id(frozen)
    )
    session, writer = _session(tmp_path)

    captured: list[Path] = []
    for value in (1.0, 2.0):
        await session.start(ProfileCaptureStart(profile="fuel-map"))
        captured.append(writer.paths.root)
        await writer.write(TelemetryRecord.obd(1_790_000_000.0 + value, "RPM", value, "rpm"))
        await session.stop("operator_stop")

    assert [path.name for path in captured] == [
        "_capture-fuel-map-2026-09-25T233019",
        "_capture-fuel-map-2026-09-25T233020",
    ]
    for path, value in zip(captured, (1.0, 2.0), strict=True):
        chunks = sorted(path.glob("telemetry-*.parquet"))
        values = [
            v for chunk in chunks for v in pq.read_table(chunk).column("value_number").to_pylist()
        ]
        assert values == [value], path.name
        manifest = read_capture_manifest(path)
        assert manifest is not None and manifest.end_reason == "operator_stop"
