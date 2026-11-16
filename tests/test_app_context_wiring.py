"""The one ``AppContext`` carries every object ``create_app`` is given (R29.6).

``create_app`` took ~45 daemon objects and respelled each at every layer below it
(the public call, the control call, ``ControlContext(...)``, one keyword call per
non-control module). A name dropped at any layer reached its routes as ``None``
and nothing failed. Now there is one record, built once, and these pin that it
is the right one:

- every ``create_app`` argument lands on the context, by identity;
- the daemon's own app (``server._build_app`` over a fully wired
  ``DaemonComponents``) hands each live component to the context, and no field a
  route can read is left ``None``;
- routes from the control package and from the non-control modules both act on
  that live object (one capture started through ``/control/captures`` is the one
  the ROM library's ``/control/rom/backup`` refuses on);
- a route module cannot take a dependency of its own: every ``register_*`` takes
  ``(app, ctx)``, and none builds the auth dependency.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.collection.capture_profiles import CaptureProfileSources, CaptureProfileStore
from src.collection.imu_collector import ImuHealth
from src.config import CanaryConfig
from src.control.audit import ControlAuditLogger
from src.control.capture import CaptureController
from src.control.profile_capture import CaptureAutostart, ProfileCaptureSession
from src.control.replay_control import ReplayController
from src.daemon import build_components
from src.daemon import server as server_mod
from src.daemon.broadcast import Broadcaster
from src.daemon.components import DaemonComponents
from src.dashboard.api import create_app
from src.dashboard.app_context import AppContext
from src.features.live import LiveFeatureSource
from src.paths import REPO_ROOT
from src.storage.async_writer import AsyncParquetTelemetryWriter
from src.storage.parquet_writer import ParquetTelemetryWriter
from tests.app_context import bare_context
from tests.configs import daemon_test_config

ROUTES = REPO_ROOT / "src" / "dashboard" / "routes"

#: ``create_app`` keyword -> the ``AppContext`` field it becomes. A new keyword
#: fails ``test_every_create_app_argument_is_accounted_for`` until it is here
#: (or in ``APP_LEVEL_ONLY``), which is the decision the old four-layer
#: spelling let a change skip.
CREATE_APP_TO_CONTEXT: dict[str, str] = {
    "anomalies_dir": "anomalies_dir",
    "models_dir": "models_dir",
    "telemetry_broadcaster": "telemetry_bus",
    "anomalies_broadcaster": "anomalies_bus",
    "scores_broadcaster": "scores_bus",
    "emitter": "emitter",
    "behavior_models_path": "behavior_models_path",
    "accepted_baselines_path": "accepted_baselines_path",
    "bindings_path": "bindings_path",
    "dids_path": "dids_path",
    "test_profiles_dir": "test_profiles_dir",
    "capture_controller": "capture_controller",
    "job_controller": "job_controller",
    "replay_controller": "replay_controller",
    "control_broadcaster": "control_bus",
    "audit_logger": "audit_logger",
    "control_token": "resolved_control_token",
    "feedback_store": "feedback_store",
    "vehicle_ledger_store": "vehicle_ledger_store",
    "alert_tuner": "alert_tuner",
    "health_monitor": "health_monitor",
    "behavior_live": "behavior_live",
    "dtc_state": "dtc_state",
    "monitor_state": "monitor_state",
    "gps_tracker": "gps_tracker",
    "imu_health": "imu_health",
    "sequence_detector_controller": "sequence_detector_controller",
    "force_poll_trigger": "force_poll_trigger",
    "storage_writer": "storage_writer",
    "capture_profile_store": "profile_store",
    "ups_status_path": "ups_status_path",
    "ups_keepalive_path": "ups_keepalive_path",
    "provision_window_path": "provision_window_path",
    "network_reader": "network_reader",
    "network_socket_path": "network_socket_path",
    "update_paths": "update_paths",
    "paired_devices": "paired_devices",
    "pairing": "pairing",
    "inference_timing": "inference_timing",
    "live_source": "live_source",
    "profile_capture": "session",
    "capture_autostart": "autostart",
}
#: Arguments that shape the ``FastAPI`` object, not what a route reads.
APP_LEVEL_ONLY = {"static_dir", "enable_dev_cors"}
#: Context fields ``create_app`` derives rather than receives: the guard from the
#: token, and the control-event publisher over the audit logger and the bus.
DERIVED = {"require_auth", "publish_control_event"}
#: Arguments that are paths; every other one is an object.
PATH_ARGUMENTS = {
    "anomalies_dir",
    "models_dir",
    "behavior_models_path",
    "accepted_baselines_path",
    "bindings_path",
    "dids_path",
    "test_profiles_dir",
    "ups_status_path",
    "ups_keepalive_path",
    "provision_window_path",
    "network_socket_path",
}
#: ``None`` in the daemon's own app by design: ``None`` is the root service's
#: default socket (``$CANARY_NETWORK_SOCKET`` or ``/run/canary/network.sock``).
NONE_IN_THE_DAEMON = {"network_socket_path"}


def _config(tmp_path: Path) -> CanaryConfig:
    config = daemon_test_config(tmp_path)
    config.collection.storage_path.mkdir(parents=True, exist_ok=True)
    return config


# ---------------------------------------------------------------- create_app


def test_every_create_app_argument_is_accounted_for() -> None:
    arguments = set(inspect.signature(create_app).parameters) - {"config"}
    assert arguments == set(CREATE_APP_TO_CONTEXT) | APP_LEVEL_ONLY


def test_every_context_field_has_a_source() -> None:
    fields = {f.name for f in dataclasses.fields(AppContext)} - {"config"}
    assert fields == set(CREATE_APP_TO_CONTEXT.values()) | DERIVED


def test_every_create_app_argument_reaches_the_context_by_identity(tmp_path: Path) -> None:
    """Each argument is a distinct sentinel, so one that is dropped, swapped for a
    neighbour or replaced by a default cannot pass."""
    given: dict[str, Any] = {
        name: (tmp_path / name) if name in PATH_ARGUMENTS else SimpleNamespace(marker=name)
        for name in CREATE_APP_TO_CONTEXT
    }
    given["control_token"] = "wiring-token"
    app = create_app(_config(tmp_path), **given, static_dir=tmp_path / "no-static")
    ctx: AppContext = app.state.app_context
    for argument, field in CREATE_APP_TO_CONTEXT.items():
        if argument == "control_token":
            assert ctx.resolved_control_token == "wiring-token"
        else:
            assert getattr(ctx, field) is given[argument], f"{argument} -> ctx.{field}"
    # What the app itself exposes is the same object, not a second one.
    assert app.state.paired_devices is ctx.paired_devices
    assert app.state.pairing is ctx.pairing
    assert app.state.telemetry_broadcaster is ctx.telemetry_bus
    assert app.state.control_broadcaster is ctx.control_bus


def test_a_bare_app_resolves_what_it_was_not_given(tmp_path: Path) -> None:
    config = _config(tmp_path)
    app = create_app(config, anomalies_dir=tmp_path / "an", static_dir=tmp_path / "none")
    ctx: AppContext = app.state.app_context
    unresolved = [
        f.name
        for f in dataclasses.fields(AppContext)
        if getattr(ctx, f.name) is None
        and f.name
        in {
            "profile_store",
            "session",
            "autostart",
            "paired_devices",
            "pairing",
            "telemetry_bus",
            "anomalies_bus",
            "scores_bus",
            "control_bus",
            "behavior_models_path",
            "require_auth",
        }
    ]
    assert unresolved == []
    assert ctx.behavior_models_path == config.daemon.behavior_models
    assert ctx.session.profile_store is ctx.profile_store


def test_a_given_capture_lifecycle_supplies_the_store_the_routes_use(tmp_path: Path) -> None:
    """R03.5: the routes and the lifecycle they drive share one store."""
    store = SimpleNamespace(marker="store")
    lifecycle = SimpleNamespace(profile_store=store)
    app = create_app(
        _config(tmp_path),
        anomalies_dir=tmp_path / "an",
        profile_capture=lifecycle,  # type: ignore[arg-type]
        static_dir=tmp_path / "none",
    )
    assert app.state.app_context.profile_store is store
    assert app.state.app_context.session is lifecycle


def test_the_control_event_publisher_writes_the_audit_log_and_the_context_bus(
    tmp_path: Path,
) -> None:
    audit = ControlAuditLogger(tmp_path / "audit.jsonl")
    bus = Broadcaster()
    app = create_app(
        _config(tmp_path),
        anomalies_dir=tmp_path / "an",
        audit_logger=audit,
        control_broadcaster=bus,
        static_dir=tmp_path / "none",
    )
    ctx: AppContext = app.state.app_context
    events = bus.subscribe()
    ctx.publish_control_event("wiring.probe", "ok", {"n": 1})
    assert events.queue.get_nowait()["action"] == "wiring.probe"
    assert "wiring.probe" in (tmp_path / "audit.jsonl").read_text()


# ------------------------------------------------------------- the daemon app


def _wired_components(tmp_path: Path, config: CanaryConfig) -> DaemonComponents:
    """``build_components`` with every optional component a running daemon has."""
    components = build_components(config, load_models=False)
    writer = ParquetTelemetryWriter(config.collection.storage_path)
    components.storage_writer = AsyncParquetTelemetryWriter(writer)
    components.capture_controller = CaptureController(lambda: writer.paths.root)
    components.replay_controller = ReplayController(
        queue=asyncio.Queue(),
        writer=writer,
        anomaly_logger=components.event_logger,
        storage_root=config.collection.storage_path,
        broadcast_stale_timeout_seconds=10.0,
    )
    components.live_source = LiveFeatureSource(config=config)
    components.imu_health = ImuHealth()
    store = CaptureProfileStore(
        CaptureProfileSources.of(config.daemon),
        config.collection,
        config.daemon.behavior_models,
        custom_pids_path=config.daemon.custom_pids,
    )
    components.profile_capture = ProfileCaptureSession(
        storage_writer=components.storage_writer,
        capture_controller=components.capture_controller,
        profile_store=store,
        health_monitor=components.health_monitor,
    )
    components.capture_autostart = CaptureAutostart(
        components.profile_capture,
        components.live_source,
        lambda: components.replay_controller,
        tick_seconds=lambda: 2.0,
    )
    return components


#: ``DaemonComponents`` attribute -> the ``AppContext`` field ``_build_app`` hands it to.
COMPONENT_TO_CONTEXT: dict[str, str] = {
    "telemetry_broadcaster": "telemetry_bus",
    "anomalies_broadcaster": "anomalies_bus",
    "scores_broadcaster": "scores_bus",
    "control_broadcaster": "control_bus",
    "emitter": "emitter",
    "capture_controller": "capture_controller",
    "job_controller": "job_controller",
    "replay_controller": "replay_controller",
    "storage_writer": "storage_writer",
    "audit_logger": "audit_logger",
    "vehicle_ledger": "vehicle_ledger_store",
    "alert_tuner": "alert_tuner",
    "health_monitor": "health_monitor",
    "behavior_live": "behavior_live",
    "dtc_state": "dtc_state",
    "monitor_state": "monitor_state",
    "gps_tracker": "gps_tracker",
    "imu_health": "imu_health",
    "sequence_detector_controller": "sequence_detector_controller",
    "force_poll_trigger": "force_poll_trigger",
    "inference_timing": "inference_timing",
    "live_source": "live_source",
    "profile_capture": "session",
    "capture_autostart": "autostart",
}


@pytest.fixture
def daemon_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, DaemonComponents]:
    config = _config(tmp_path)
    monkeypatch.setenv(config.daemon.control_token_env, "secret")
    components = _wired_components(tmp_path, config)
    return server_mod._build_app(components), components


def test_the_daemons_app_hands_each_live_component_to_the_context(
    daemon_app: tuple[FastAPI, DaemonComponents],
) -> None:
    app, components = daemon_app
    ctx: AppContext = app.state.app_context
    for attribute, field in COMPONENT_TO_CONTEXT.items():
        live = getattr(components, attribute)
        assert live is not None, f"the fixture left components.{attribute} unset"
        assert getattr(ctx, field) is live, f"components.{attribute} -> ctx.{field}"
    config = components.config
    assert ctx.config is config
    assert ctx.anomalies_dir == config.daemon.anomalies_dir
    assert ctx.models_dir == config.daemon.models_dir
    assert ctx.behavior_models_path == config.daemon.behavior_models
    assert ctx.accepted_baselines_path == config.daemon.accepted_baselines
    assert ctx.resolved_control_token == "secret"
    assert ctx.profile_store is components.profile_capture.profile_store  # type: ignore[union-attr]
    assert ctx.feedback_store is not None and ctx.network_reader is not None


def test_no_field_a_route_can_read_is_none_in_the_daemons_app(
    daemon_app: tuple[FastAPI, DaemonComponents],
) -> None:
    """The failure the four-layer spelling allowed: a route handed ``None`` for an
    object the daemon has. Every field is set, bar the one that is ``None`` by design."""
    app, _components = daemon_app
    ctx: AppContext = app.state.app_context
    unset = sorted(
        f.name
        for f in dataclasses.fields(AppContext)
        if getattr(ctx, f.name) is None and f.name not in NONE_IN_THE_DAEMON
    )
    assert unset == []


def test_the_daemons_routes_act_on_the_live_components(
    daemon_app: tuple[FastAPI, DaemonComponents],
) -> None:
    """Handlers, not just fields: each reads the object the daemon owns."""
    app, components = daemon_app
    auth = {"Authorization": "Bearer secret"}
    client = TestClient(app)

    # catalog_reads (public): the cached DTC/monitor state and the behavior overlay.
    components.dtc_state.snapshot = lambda **_k: {"marker": "dtc"}  # type: ignore[method-assign]
    components.monitor_state.snapshot = lambda **_k: {"marker": "monitors"}  # type: ignore[method-assign]
    components.behavior_live.snapshot = lambda **_k: {"marker": "behavior"}  # type: ignore[method-assign]
    assert client.get("/obd/dtc").json() == {"marker": "dtc"}
    assert client.get("/obd/monitors").json() == {"marker": "monitors"}
    assert client.get("/models/behavior/live").json() == {"marker": "behavior"}

    # control package: start a capture window through the route...
    events = components.control_broadcaster.subscribe()
    started = client.post("/control/captures", json={"purpose": "wiring-probe"}, headers=auth)
    assert started.status_code == 201, started.text
    assert components.capture_controller.current_capture() is not None  # type: ignore[union-attr]
    # ...and a non-control module sees that same controller (rom_library).
    backup = client.get("/control/rom/backup", headers=auth).json()
    capture = next(c for c in backup["preconditions"] if c["id"] == "capture")
    assert capture["ok"] is False and "wiring-probe" in capture["detail"]
    # The event the control route published rode the daemon's own bus.
    assert events.queue.get_nowait()["action"] == "capture.start"


# --------------------------------------------------------------- auth + shape


def test_the_context_builds_its_auth_from_its_token() -> None:
    """``require_auth`` is derived, so a context cannot carry a token and a guard
    that disagree: ``replace`` rebuilds it."""
    config = daemon_test_config(Path("/nonexistent-wiring-test"))
    ctx = bare_context(config, token="alpha")
    other = dataclasses.replace(ctx, resolved_control_token="beta")
    assert other.require_auth is not ctx.require_auth

    def serve(context: AppContext) -> TestClient:
        app = FastAPI()
        router = context.router()

        @router.get("/guarded")
        def guarded() -> dict[str, bool]:
            return {"ok": True}

        app.include_router(router)
        return TestClient(app)

    assert serve(ctx).get("/guarded", headers={"Authorization": "Bearer alpha"}).status_code == 200
    assert serve(ctx).get("/guarded", headers={"Authorization": "Bearer beta"}).status_code == 401
    assert serve(other).get("/guarded", headers={"Authorization": "Bearer beta"}).status_code == 200


def _route_module_functions() -> list[tuple[str, ast.FunctionDef]]:
    found: list[tuple[str, ast.FunctionDef]] = []
    for path in sorted(ROUTES.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found.extend(
            (f"{path.relative_to(ROUTES)}:{node.name}", node)
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name.startswith("register_")
        )
    return found


#: ``register_companion_link_routes`` keeps its test seams (the Pi round trips);
#: they are collaborators, not daemon objects.
KEYWORD_SEAMS = {
    "companion_link.py:register_companion_link_routes": {
        "lib",
        "discover_fn",
        "probe_fn",
        "request_json",
        "download_session_export",
        "import_archive",
        "environ",
    }
}


def test_every_registrar_takes_the_app_and_the_context_and_nothing_else() -> None:
    registrars = _route_module_functions()
    assert len(registrars) >= 12, [name for name, _ in registrars]
    for name, node in registrars:
        positional = [a.arg for a in node.args.args]
        keyword = {a.arg for a in node.args.kwonlyargs}
        assert positional == ["app", "ctx"], f"{name} takes {positional}"
        assert keyword <= KEYWORD_SEAMS.get(name, set()), f"{name} takes keyword {keyword}"


def test_no_route_module_builds_an_auth_dependency_of_its_own() -> None:
    """Auth is ``ctx.router()`` / ``ctx.require_auth``: the token lives on the
    context, so no module can register a guarded route with another one."""
    offenders = sorted(
        str(path.relative_to(ROUTES))
        for path in ROUTES.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "control_auth_dependency"
    )
    assert offenders == []
