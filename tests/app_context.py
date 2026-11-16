"""A bare ``AppContext`` for tests that mount one route registrar (R29.6).

Every ``register_*`` takes the one :class:`~src.dashboard.app_context.AppContext`
``create_app`` builds. A test that wants a single registrar on its own
``FastAPI()`` needs a context without the whole app: this fills every field
``create_app`` always resolves with the value a bare app (no daemon) gets, and
takes the rest by name::

    app = FastAPI()
    register_workbench_routes(app, bare_context(config, bindings_path=BINDINGS))

A field that is not on ``AppContext`` fails here, so a test cannot misspell a
dependency into a silent default.
"""

from __future__ import annotations

from typing import Any

from src.collection.capture_profiles import CaptureProfileSources, CaptureProfileStore
from src.collection.kline.plan import KlineCatalog
from src.config import CanaryConfig
from src.control.key_cycle_pairing import KeyCyclePairing
from src.control.paired_devices import PairedDevices
from src.control.paired_devices import default_path as paired_devices_default_path
from src.control.profile_capture import (
    AUTOSTART_TICK_SECONDS,
    CaptureAutostart,
    ProfileCaptureSession,
)
from src.control.updater import DEFAULT_UPDATE_PATHS
from src.daemon.broadcast import Broadcaster
from src.daemon.ups import DEFAULT_UPS_KEEPALIVE_PATH, DEFAULT_UPS_STATUS_PATH
from src.dashboard.app_context import AppContext
from src.dashboard.paths import DEFAULT_BINDINGS_PATH, DEFAULT_DIDS_PATH, DEFAULT_TEST_PROFILES_DIR
from src.provisioning.gate import DEFAULT_WINDOW_PATH


def bare_context(
    config: CanaryConfig, *, token: str | None = "secret", **fields: Any
) -> AppContext:
    """The context ``create_app(config, ...)`` builds for a bare app, plus ``fields``."""
    provision_window_path = fields.get("provision_window_path", DEFAULT_WINDOW_PATH)
    profile_store = fields.get("profile_store") or CaptureProfileStore(
        CaptureProfileSources.of(config.daemon),
        config.collection,
        config.daemon.behavior_models,
        custom_pids_path=config.daemon.custom_pids,
        kline_catalog=KlineCatalog.from_config(config),
    )
    session = fields.get("session") or ProfileCaptureSession(
        storage_writer=fields.get("storage_writer"),
        capture_controller=fields.get("capture_controller"),
        profile_store=profile_store,
        health_monitor=fields.get("health_monitor"),
    )
    paired_devices = fields.get("paired_devices") or PairedDevices(
        paired_devices_default_path(config.daemon.state_dir)
    )
    defaults: dict[str, Any] = {
        "anomalies_dir": config.daemon.anomalies_dir,
        "behavior_models_path": config.daemon.behavior_models,
        "accepted_baselines_path": config.daemon.accepted_baselines,
        "bindings_path": DEFAULT_BINDINGS_PATH,
        "dids_path": DEFAULT_DIDS_PATH,
        "test_profiles_dir": DEFAULT_TEST_PROFILES_DIR,
        "ups_status_path": DEFAULT_UPS_STATUS_PATH,
        "ups_keepalive_path": DEFAULT_UPS_KEEPALIVE_PATH,
        "provision_window_path": provision_window_path,
        "update_paths": DEFAULT_UPDATE_PATHS,
        "publish_control_event": lambda action, result, payload: None,
        "telemetry_bus": Broadcaster(),
        "anomalies_bus": Broadcaster(),
        "scores_bus": Broadcaster(),
        "control_bus": Broadcaster(),
        "profile_store": profile_store,
        "session": session,
        "autostart": CaptureAutostart(
            session,
            fields.get("live_source"),
            lambda: fields.get("replay_controller"),
            tick_seconds=lambda: AUTOSTART_TICK_SECONDS,
        ),
        "paired_devices": paired_devices,
        "pairing": KeyCyclePairing(paired_devices, lambda: None, window_path=provision_window_path),
    }
    return AppContext(config=config, resolved_control_token=token, **{**defaults, **fields})


__all__ = ["bare_context"]
