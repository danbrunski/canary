from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.feedback import FeedbackStore, annotate_events_with_feedback
from src.analysis.gps_tracker import GpsTracker
from src.config import CanaryConfig
from src.control.replay_control import ReplayController
from src.dashboard.behavior_live import BehaviorLiveState
from src.dashboard.readers import (
    echo_config,
    read_anomalies_since,
    read_behavior_models,
    read_bindings_for_display,
    read_can_coverage,
    read_latest_per_channel,
    read_latest_telemetry_rows,
    read_obd_coverage,
)
from src.dashboard.status import build_status
from src.models.health import HealthMonitor


def register_public_routes(
    app: FastAPI,
    *,
    config: CanaryConfig,
    anomalies_dir: Path,
    models_dir: Path | None,
    emitter: AnomalyEventEmitter | None,
    feedback_store: FeedbackStore | None,
    behavior_models_path: Path,
    bindings_path: Path,
    dids_path: Path,
    test_profiles_dir: Path,
    replay_controller: ReplayController | None,
    health_monitor: HealthMonitor | None,
    behavior_live: BehaviorLiveState | None,
    gps_tracker: GpsTracker | None,
) -> None:
    @app.get("/status")
    def get_status() -> dict[str, Any]:
        payload = build_status(
            config,
            models_dir=models_dir,
            emitter=emitter,
            health_monitor=health_monitor,
            replay_controller=replay_controller,
            gps_tracker=gps_tracker,
        ).to_dict()
        if replay_controller is not None:
            payload["replay"] = replay_controller.status().to_dict()
        return payload

    @app.get("/config")
    def get_config() -> dict[str, Any]:
        return echo_config(config)

    @app.get("/anomalies")
    def get_anomalies(since: str | None = Query(default=None)) -> dict[str, Any]:
        try:
            events = read_anomalies_since(anomalies_dir, since=since)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"bad `since`: {exc}") from exc
        # Join the operator's verdict onto each event so the timeline can
        # show "you marked this" without a second round-trip (Task 6.24).
        if feedback_store is not None:
            events = annotate_events_with_feedback(events, feedback_store)
        return {"events": events, "count": len(events)}

    @app.get("/telemetry/latest")
    def get_telemetry_latest(
        n: int = Query(default=100, ge=1, le=10_000),
        per_channel: bool = Query(default=False),
    ) -> dict[str, Any]:
        rows = (
            read_latest_per_channel(config.collection.storage_path)
            if per_channel
            else read_latest_telemetry_rows(config.collection.storage_path, n=n)
        )
        return {"rows": rows, "count": len(rows)}

    @app.get("/models/behavior")
    def get_behavior_models() -> dict[str, Any]:
        # Re-read on every request so a `scripts/refit_behavior_models.py`
        # run lands in the dashboard without a daemon restart. Cheap — the
        # file is < 100 KB even with a full surface catalog.
        return read_behavior_models(behavior_models_path)

    @app.get("/models/behavior/live")
    def get_behavior_live() -> dict[str, Any]:
        # Live "you are here" overlay for Models▸Behavior: per-model
        # expected/actual/deviation/severity + a short server-side trail,
        # fed by the daemon's inference loop. Empty (not an error) when no
        # daemon is wired — the static surfaces still render standalone.
        if behavior_live is None:
            return {"observed": 0, "timestamp": None, "operating_mode": None, "models": {}}
        return behavior_live.snapshot()

    @app.get("/bindings")
    def get_bindings() -> dict[str, Any]:
        # Re-read per request so a hand-edit to bindings.yaml (during RE)
        # surfaces in the dashboard immediately. The file is small.
        return read_bindings_for_display(bindings_path)

    @app.get("/dids")
    def get_dids() -> dict[str, Any]:
        # Read-only Nissan Mode-22 DID catalog for the 6.16 workbench.
        # Re-read per request like /bindings so a hand-edit during RE shows
        # up immediately; empty doc when the file is absent.
        from src.decode.dids import load_did_map

        if not dids_path.exists():
            return {"schema_version": None, "vehicle": {}, "access": None, "dids": []}
        return load_did_map(dids_path).model_dump(mode="json")

    @app.get("/test-profiles")
    def get_test_profiles() -> dict[str, Any]:
        # Read-only catalog for the Test panel, scanned per request so a
        # dropped-in / hand-edited profile YAML appears without a daemon
        # restart. Unauthenticated like /dids + /bindings (no secrets;
        # listing profiles must work before the control token is entered).
        from src.control.test_profiles import load_test_profiles

        return load_test_profiles(test_profiles_dir).model_dump(mode="json")

    @app.get("/obd/coverage")
    def get_obd_coverage() -> dict[str, Any]:
        # Read-only honesty surface (Task 6.3 / Risk R7): which public PIDs
        # the ECU supports, which CANary decodes, which are undecoded gaps.
        return read_obd_coverage(config.collection.storage_path)

    @app.get("/can/coverage")
    def get_can_coverage() -> dict[str, Any]:
        # Static catalog of decodable broadcast-CAN signals (bindings + DBC)
        # with their source CAN IDs. The CAN tab overlays live values and
        # discovered/undecoded IDs from the telemetry stream client-side.
        return read_can_coverage(bindings_path)
