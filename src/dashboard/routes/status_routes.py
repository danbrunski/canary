"""The live-state reads: who this daemon is, ``/status``, the config echo, recent anomalies and the
telemetry seed.

Public (no token), like the rest of :func:`~src.dashboard.routes.public.register_public_routes`.
R29.7 moved these out of that one 383-line function; ``/status`` has its own payload builder
(:mod:`src.dashboard.status`), so this is the route module, not a second builder.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Query

from src.alerts.emitter import AnomalyEventEmitter
from src.analysis.gps_tracker import GpsTracker
from src.behavior.accepted_baselines import DEFAULT_ACCEPTED_BASELINES_PATH
from src.collection.imu_collector import ImuHealth
from src.config import CanaryConfig
from src.control.replay_control import ReplayController
from src.control.sequence_detector import SequenceDetectorController
from src.daemon.ups import DEFAULT_UPS_STATUS_PATH
from src.dashboard.identity import identity_payload
from src.dashboard.readers import (
    decoded_did_rows,
    echo_config,
    read_accepted_baselines,
    read_anomalies_since,
    read_latest_per_channel,
    read_latest_telemetry_rows,
)
from src.dashboard.status import (
    StatusView,
    VehicleStateCache,
    build_status,
    compact_status,
    gather_status_sources,
)
from src.models.feedback import FeedbackStore, annotate_events_with_feedback
from src.provisioning.reader import NetworkStatusReader

if TYPE_CHECKING:
    from src.daemon.health import HealthMonitor
    from src.daemon.inference_metrics import InferenceTimingRecorder


def register_status_routes(
    app: FastAPI,
    *,
    config: CanaryConfig,
    anomalies_dir: Path,
    models_dir: Path | None,
    emitter: AnomalyEventEmitter | None,
    feedback_store: FeedbackStore | None,
    dids_path: Path,
    replay_controller: ReplayController | None,
    health_monitor: HealthMonitor | None,
    gps_tracker: GpsTracker | None = None,
    imu_health: ImuHealth | None = None,
    sequence_detector_controller: SequenceDetectorController | None = None,
    accepted_baselines_path: Path = DEFAULT_ACCEPTED_BASELINES_PATH,
    storage_writer: Any = None,
    inference_timing: InferenceTimingRecorder | None = None,
    live_source: Any = None,
    ups_status_path: Path = DEFAULT_UPS_STATUS_PATH,
    network_reader: NetworkStatusReader | None = None,
) -> None:
    # Owned here rather than passed in, so the vehicle-state derivation is
    # shared by both /status views of every app without any caller having to
    # remember to wire it (and never shared *between* apps, which would leak
    # one test's truck state into the next).
    vehicle_state_cache = VehicleStateCache()

    @app.get("/identity")
    def get_identity() -> dict[str, Any]:
        """Who this daemon is, for discovery on the network (no token, no state).

        ``src.companion.discovery`` sweeps a LAN with this. It is a few
        string fields built from the config and the host, never the full
        ``/status``, so a sweep of 254 addresses stays cheap for the Pi.
        """
        return identity_payload(config)

    @app.get("/status")
    def get_status(view: StatusView = "full") -> dict[str, Any]:
        """Daemon snapshot. ``view=compact`` is the pollable projection.

        The full payload costs 1.3–2.6 s on the Pi and carries the whole
        ``obd_scheduler`` block (per-channel achieved rates, every request's
        state, discovery), so polling it capped health resolution at ~20 s and
        put ±2,700 CAN frames of slack into D01.5's frame-loss bracket. The
        compact view answers "is it healthy, and what are the counters right
        now" from the *same* source objects — see
        :func:`src.dashboard.status.compact_status`. Both views are built from
        one :class:`~src.dashboard.status.StatusSources`, so they can never
        report different numbers for the same instant.
        """
        sources = gather_status_sources(
            config,
            emitter=emitter,
            health_monitor=health_monitor,
            replay_controller=replay_controller,
            storage_writer=storage_writer,
            inference_timing=inference_timing,
            vehicle_state_cache=vehicle_state_cache,
            live_source=live_source,
            imu_health=imu_health,
        )
        if view == "compact":
            return compact_status(sources)
        payload = build_status(
            config,
            models_dir=models_dir,
            emitter=emitter,
            health_monitor=health_monitor,
            replay_controller=replay_controller,
            gps_tracker=gps_tracker,
            storage_writer=storage_writer,
            inference_timing=inference_timing,
            sources=sources,
            ups_status_path=ups_status_path,
            dids_path=dids_path,
        ).to_dict()
        if replay_controller is not None:
            payload["replay"] = replay_controller.status().to_dict()
        if sequence_detector_controller is not None:
            payload["sequence_detector"] = sequence_detector_controller.status().to_dict()
        # 2.3 — "N accepted-baseline overrides active", always visible; the
        # StatusStrip reads `.active`, the expandable detail reads
        # `.overrides` (each with provenance + `review_by` + `expired`).
        # Re-read every request, same posture as /models/behavior — the
        # file is tiny and an operator's edit should show up without a
        # daemon restart.
        payload["accepted_baseline_overrides"] = read_accepted_baselines(accepted_baselines_path)
        # P2.3 — the address a phone should bookmark. ``peek`` never waits on
        # nmcli: a stale copy (its ``age_s`` says how stale) is served while a
        # background refresh replaces it, at most every STATUS_MAX_AGE_S.
        reading = network_reader.peek() if network_reader is not None else None
        payload["network"] = (
            None if reading is None else reading.to_dict(dashboard_port=config.dashboard.port)
        )
        return payload

    @app.get("/config")
    def get_config() -> dict[str, Any]:
        return echo_config(config)

    @app.get("/anomalies")
    def get_anomalies(
        since: str | None = Query(default=None),
        source: str | None = Query(default=None),
        mode: str | None = Query(default=None),
        limit: int | None = Query(default=None, ge=1),
    ) -> dict[str, Any]:
        try:
            events = read_anomalies_since(
                anomalies_dir, since=since, source=source, mode=mode, limit=limit
            )
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
        # Mode-22 polls are stored raw (``obd:DID_<hex>`` + a hex payload);
        # decode them here so this seed carries the same ``did:<NAME>`` rows
        # the live stream publishes. Without it a page load shows no DIDs
        # until the next poll window — up to a minute of blank gauges,
        # since the DID session is mutually exclusive with Mode 01 and so
        # runs in windows rather than continuously.
        rows = rows + decoded_did_rows(rows, dids_path)
        return {"rows": rows, "count": len(rows)}
