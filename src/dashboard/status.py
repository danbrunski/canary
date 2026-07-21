"""Dashboard status snapshot assembly."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from src.alerts.emitter import AnomalyEventEmitter
from src.analysis.gps_tracker import GpsTracker
from src.config import CanaryConfig
from src.control.replay_control import ReplayController
from src.models.health import HealthMonitor
from src.preprocessing.loader import discover_sessions
from src.timefmt import parse_iso_utc

VehicleStateName = Literal[
    "engine_running",
    "ignition_only",
    "engine_off",
    "no_data",
    "replay",
    "unknown",
]

#: A channel that's been quiet longer than this is treated as "not flowing"
#: for the vehicle-state classification. OBD polls at ~1 Hz so 10 s is well
#: past the slowest realistic refresh.
_FRESHNESS_THRESHOLD_SECONDS = 10.0

#: Below this RPM (with OBD fresh) we call it "ignition_only / KOEO" rather
#: than "engine running" — most idle 4-cyl gas engines sit around 700–800,
#: so 400 is well below any actually-running state but above sensor jitter.
_RPM_RUNNING_THRESHOLD = 400.0


@dataclass(frozen=True)
class VehicleState:
    """Derived operational state — what's the truck doing right now?

    Surfaced in ``GET /status.vehicle_state`` so dashboards / agents stop
    guessing whether "no anomalies" means the detectors are broken vs. the
    engine just isn't running. The class is also documented in
    ``CLAUDE.md`` because the live-vs-engine-off-vs-replay confusion has
    been a recurring source of wrong conclusions.
    """

    state: VehicleStateName
    rpm: float | None = None
    coolant_temp: float | None = None
    run_time: float | None = None
    obd_freshness_seconds: float | None = None
    can_freshness_seconds: float | None = None
    replay_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        def _round(value: float | None) -> float | None:
            return None if value is None else round(value, 2)

        return {
            "state": self.state,
            "rpm": _round(self.rpm),
            "coolant_temp": _round(self.coolant_temp),
            "run_time": _round(self.run_time),
            "obd_freshness_seconds": _round(self.obd_freshness_seconds),
            "can_freshness_seconds": _round(self.can_freshness_seconds),
            "replay_id": self.replay_id,
        }


def classify_vehicle_state(
    *,
    rpm: float | None,
    coolant_temp: float | None,
    run_time: float | None,
    obd_freshness_seconds: float | None,
    can_freshness_seconds: float | None,
    replay_id: str | None = None,
    freshness_threshold_seconds: float = _FRESHNESS_THRESHOLD_SECONDS,
    rpm_running_threshold: float = _RPM_RUNNING_THRESHOLD,
) -> VehicleState:
    """Classify the live data source from a few derived signals.

    Pure function so the rule table is testable without spinning up the
    daemon. The I/O side (Parquet reads, replay-status fetch) lives in
    :func:`build_status`.

    Precedence:

    1. ``replay`` overrides everything when a replay is running, even if
       OBD/CAN telemetry is also flowing — the replayed records ARE the
       OBD/CAN telemetry from the agent's perspective.
    2. If neither OBD nor CAN have refreshed within
       ``freshness_threshold_seconds``, ``no_data`` (Pi probably
       disconnected or daemon idle).
    3. RPM above ``rpm_running_threshold`` (with OBD fresh) →
       ``engine_running``.
    4. OBD fresh but RPM == 0 → ``ignition_only`` (key on, engine off —
       OBD responds, RUN_TIME is 0).
    5. OBD stale but CAN fresh → ``engine_off`` (plugged into the bus
       but the ECM isn't responding to OBD, typical of full power-down).
    6. Anything else → ``unknown`` (caller should still trust the raw
       fields).
    """
    if replay_id is not None:
        return VehicleState(
            state="replay",
            rpm=rpm,
            coolant_temp=coolant_temp,
            run_time=run_time,
            obd_freshness_seconds=obd_freshness_seconds,
            can_freshness_seconds=can_freshness_seconds,
            replay_id=replay_id,
        )

    obd_fresh = (
        obd_freshness_seconds is not None and obd_freshness_seconds <= freshness_threshold_seconds
    )
    can_fresh = (
        can_freshness_seconds is not None and can_freshness_seconds <= freshness_threshold_seconds
    )

    if not obd_fresh and not can_fresh:
        state: VehicleStateName = "no_data"
    elif obd_fresh and rpm is not None and rpm >= rpm_running_threshold:
        state = "engine_running"
    elif obd_fresh and (rpm is None or rpm == 0):
        state = "ignition_only"
    elif can_fresh and not obd_fresh:
        state = "engine_off"
    else:
        state = "unknown"

    return VehicleState(
        state=state,
        rpm=rpm,
        coolant_temp=coolant_temp,
        run_time=run_time,
        obd_freshness_seconds=obd_freshness_seconds,
        can_freshness_seconds=can_freshness_seconds,
    )


@dataclass(frozen=True)
class StatusSnapshot:
    """What ``GET /status`` returns. Stable shape independent of FastAPI."""

    storage_path: str
    free_mb: float
    latest_session: str | None
    latest_session_path: str | None
    model_versions: dict[str, str | None]
    vehicle: str
    #: Inference-health block (Task 6.27) — latency, freshness, missing-feature
    #: rate, model-swap outcomes, host load, drift — or ``None`` when the
    #: daemon hasn't wired a monitor in (tests / bare app).
    health: dict[str, Any] | None = None
    #: Emitter counters at ``/status`` request time — ``{"emitted": N,
    #: "suppressed_not_anomaly": N, "suppressed_dedupe": N}`` or ``None`` when
    #: the daemon hasn't wired an emitter in. The dashboard surfaces this as
    #: "is the anomaly path firing?".
    emitter_counters: dict[str, int] | None = None
    #: Derived operational state (engine_running, ignition_only,
    #: engine_off, no_data, replay) — see :class:`VehicleState`.
    vehicle_state: VehicleState | None = None
    #: Latest GPS fix snapshot (fix quality, satellites, lat/long/alt/speed/
    #: heading, age) or ``None`` when no GPS is configured/observed.
    gps: dict[str, Any] | None = None
    #: Cumulative trip odometer (distance + moving time) from the GPS stream,
    #: or ``None`` when no GPS is configured.
    trip: dict[str, Any] | None = None
    #: Active vehicle profile ``{slug, name, vin}`` (multi-vehicle support), or
    #: ``None`` when the daemon isn't running on a profile-scoped config (bare
    #: app / tests). ``name`` is the operator-editable display label.
    profile: dict[str, str | None] | None = None
    #: Per-detector health beyond the aggregate ``health`` block (Task 2.1,
    #: 4.7). IF's ``blind_modes`` (operating modes whose trained feature
    #: schema has gone permanently missing from the live stream, so emits
    #: are suppressed for them) + the per-mode missing-feature rate gauge
    #: that feeds it; ``segmenter.fallback_active`` (whether the live
    #: segmenter is currently stuck on constant "all" mode because its
    #: input columns are missing, so every mode-gated behavior surface is
    #: silently suppressed). ``None`` when no health monitor is wired.
    detectors: dict[str, Any] | None = None
    #: Per-loaded-model provenance (Task 6.5), keyed like ``model_versions``:
    #: ``model_version``/``created_at``/``training_data_hash``/
    #: ``training_sessions``/``artifact_format_version`` straight off each
    #: manifest — what exactly is being served and when it was trained,
    #: without an operator having to `scp`/read the manifest by hand.
    model_provenance: dict[str, dict[str, Any] | None] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "storage_path": self.storage_path,
            "free_mb": round(self.free_mb, 1),
            "latest_session": self.latest_session,
            "latest_session_path": self.latest_session_path,
            "model_versions": self.model_versions,
            "vehicle": self.vehicle,
            "emitter_counters": self.emitter_counters,
            "health": self.health,
            "vehicle_state": (None if self.vehicle_state is None else self.vehicle_state.to_dict()),
            "gps": self.gps,
            "trip": self.trip,
            "profile": self.profile,
            "detectors": self.detectors,
            "model_provenance": self.model_provenance,
        }


def _free_mb(path: Path) -> float:
    """Free disk MB at ``path`` (or its first existing parent). 0 on failure."""
    target = path
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        usage = shutil.disk_usage(target)
    except OSError:
        return 0.0
    return usage.free / (1024 * 1024)


def _model_version(manifest_path: Path) -> str | None:
    """Read the version field from a model manifest JSON, or ``None``."""
    if not manifest_path.exists():
        return None
    try:
        data = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    version = (
        data.get("model_version")
        or data.get("version")
        or data.get("training", {}).get("model_version")
    )
    return str(version) if version is not None else None


def _model_provenance(manifest_path: Path) -> dict[str, Any] | None:
    """Read the provenance fields off a model manifest JSON, or ``None``.

    Deliberately as forgiving as :func:`_model_version` (plain ``json.loads``
    + ``.get``, not the strict :func:`src.models.manifest.validate_manifest_common`
    gate) — a malformed/partial manifest shouldn't take ``/status`` down, it
    should just show up as an absent field for an operator to notice.
    """
    if not manifest_path.exists():
        return None
    try:
        data = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return {
        "model_version": data.get("model_version"),
        "created_at": data.get("created_at"),
        "training_data_hash": data.get("training_data_hash"),
        "training_sessions": data.get("training_sessions", []),
        "artifact_format_version": data.get("artifact_format_version"),
    }


def _resolve_latest_session(storage_path: Path) -> Path | None:
    sessions = discover_sessions(storage_path)
    return sessions[-1] if sessions else None


def _derive_vehicle_state(
    storage_path: Path,
    *,
    replay_controller: ReplayController | None,
    now: datetime | None = None,
) -> VehicleState:
    """Read latest-per-channel + replay state and run the classifier.

    Reads only the newest session's Parquet via :func:`read_latest_per_channel`,
    which is what the rest of the dashboard already uses — cheap enough for
    /status' polling cadence. On any I/O failure we return ``state="unknown"``
    rather than 500-ing — operators routinely hit /status to *diagnose*
    storage issues, so it shouldn't be the thing that breaks first.
    """
    replay_id: str | None = None
    if replay_controller is not None:
        replay_state = replay_controller.status()
        if replay_state.running:
            replay_id = replay_state.replay_id

    now_dt = datetime.now(UTC) if now is None else now
    rpm: float | None = None
    coolant: float | None = None
    run_time: float | None = None
    obd_latest_ts: datetime | None = None
    can_latest_ts: datetime | None = None

    # Deferred import: src.dashboard.readers imports _resolve_latest_session
    # from this module, so importing it at module load creates a cycle.
    from src.dashboard.readers import read_latest_per_channel

    try:
        rows = read_latest_per_channel(storage_path)
    except (OSError, ValueError):
        rows = []

    for row in rows:
        source = row.get("source")
        channel = row.get("channel")
        value = row.get("value")
        ts_raw = row.get("timestamp")
        ts: datetime | None = None
        if isinstance(ts_raw, str):
            try:
                ts = parse_iso_utc(ts_raw)
            except ValueError:
                ts = None
        elif isinstance(ts_raw, datetime):
            ts = ts_raw if ts_raw.tzinfo else ts_raw.replace(tzinfo=UTC)

        if source == "obd":
            if ts is not None and (obd_latest_ts is None or ts > obd_latest_ts):
                obd_latest_ts = ts
            if isinstance(value, (int, float)):
                if channel == "RPM":
                    rpm = float(value)
                elif channel == "COOLANT_TEMP":
                    coolant = float(value)
                elif channel == "RUN_TIME":
                    run_time = float(value)
        elif source == "can":
            if ts is not None and (can_latest_ts is None or ts > can_latest_ts):
                can_latest_ts = ts

    obd_freshness = (now_dt - obd_latest_ts).total_seconds() if obd_latest_ts is not None else None
    can_freshness = (now_dt - can_latest_ts).total_seconds() if can_latest_ts is not None else None

    return classify_vehicle_state(
        rpm=rpm,
        coolant_temp=coolant,
        run_time=run_time,
        obd_freshness_seconds=obd_freshness,
        can_freshness_seconds=can_freshness,
        replay_id=replay_id,
    )


def _active_profile(config: CanaryConfig) -> dict[str, str | None] | None:
    """The active vehicle profile ``{slug, name, vin}``.

    The slug is recoverable as the scoped storage path's final component (see
    ``src.profiles.scope_config``); name/vin come from the registry, falling
    back to the ``vehicle:`` block's derived label for the default profile that
    may not be registered yet.
    """
    slug = config.collection.storage_path.name
    fallback_name = f"{config.vehicle.year} {config.vehicle.make} {config.vehicle.model}".strip()
    try:
        from src.profiles import load_registry

        for profile in load_registry(config.profiles.registry_path):
            if profile.slug == slug:
                return {"slug": profile.slug, "name": profile.name, "vin": profile.vin}
    except Exception:  # noqa: BLE001 — status must never fail on a bad registry
        return None
    return {"slug": slug, "name": fallback_name, "vin": config.vehicle.vin or None}


def build_status(
    config: CanaryConfig,
    *,
    models_dir: Path | None = None,
    emitter: AnomalyEventEmitter | None = None,
    health_monitor: HealthMonitor | None = None,
    replay_controller: ReplayController | None = None,
    gps_tracker: GpsTracker | None = None,
) -> StatusSnapshot:
    """Assemble the status snapshot used by ``GET /status``."""
    storage = config.collection.storage_path
    latest = _resolve_latest_session(storage)
    model_versions: dict[str, str | None] = {}
    model_provenance: dict[str, dict[str, Any] | None] = {}
    if models_dir is not None:
        # Artifacts live one dir down: <models_dir>/<name>/model.manifest.json
        # (matches daemon.isolation_forest_artifact / lstm_artifact). Recurse so
        # the nested layout is found; fall back to the flat
        # <models_dir>/<name>.manifest.json convention when the manifest sits
        # directly in models_dir.
        for manifest in sorted(models_dir.rglob("*.manifest.json")):
            name = (
                manifest.stem.removesuffix(".manifest")
                if manifest.parent == models_dir
                else manifest.parent.name
            )
            model_versions[name] = _model_version(manifest)
            model_provenance[name] = _model_provenance(manifest)
    detectors: dict[str, Any] | None = None
    if health_monitor is not None:
        detectors = {
            "isolation_forest": {
                "blind_modes": list(health_monitor.if_blind_modes),
                "missing_feature_rate_by_mode": dict(
                    health_monitor.if_missing_feature_rate_by_mode
                ),
            },
            # 4.7 — complements the load-time unknown_operating_modes warning:
            # this is the *runtime* version, when the live segmenter can't
            # run at all (its input columns are missing) rather than being
            # configured for a mode set that doesn't match.
            "segmenter": {
                "fallback_active": health_monitor.segmenter_fallback_active,
            },
        }
    return StatusSnapshot(
        storage_path=str(storage),
        free_mb=_free_mb(storage),
        latest_session=latest.name if latest else None,
        latest_session_path=str(latest) if latest else None,
        model_versions=model_versions,
        vehicle=f"{config.vehicle.year} {config.vehicle.make} {config.vehicle.model}".strip(),
        emitter_counters=emitter.counters.to_dict() if emitter is not None else None,
        health=health_monitor.snapshot() if health_monitor is not None else None,
        vehicle_state=_derive_vehicle_state(storage, replay_controller=replay_controller),
        gps=gps_tracker.gps_status() if gps_tracker is not None else None,
        trip=gps_tracker.trip_status() if gps_tracker is not None else None,
        profile=_active_profile(config),
        detectors=detectors,
        model_provenance=model_provenance if models_dir is not None else None,
    )


__all__ = [
    "StatusSnapshot",
    "VehicleState",
    "VehicleStateName",
    "build_status",
    "classify_vehicle_state",
]
