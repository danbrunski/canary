"""Dashboard status snapshot assembly."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from src.alerts.emitter import AnomalyEventEmitter
from src.analysis.gps_tracker import GpsTracker
from src.collection import bus_budget as bus_budget_gate
from src.collection import tuning_profile, tx_gate
from src.collection.imu_collector import ImuHealth
from src.config import CanaryConfig
from src.control.replay_control import ReplayController
from src.control.tuning_readiness import build_tuning_readiness
from src.daemon.ups import (
    DEFAULT_UPS_STATUS_PATH,
    build_power_snapshot,
    read_keepalive_remaining,
    read_watchdog_status,
)
from src.models.feature_spec_health import load_audit_snapshot, structural_blindness_snapshot
from src.storage.capture_manifest import latest_capture_summary
from src.storage.monitor import free_space_mb
from src.storage.sidecar import read_json_object

# R03.1: the classifier lives in src.vehicle_state; these names are
# re-exported for the routes, tests and older imports that reach them here.
from src.vehicle_state import FRESHNESS_THRESHOLD_SECONDS as FRESHNESS_THRESHOLD_SECONDS
from src.vehicle_state import RPM_RUNNING_THRESHOLD as RPM_RUNNING_THRESHOLD
from src.vehicle_state import VEHICLE_STATE_TTL_SECONDS as VEHICLE_STATE_TTL_SECONDS
from src.vehicle_state import VehicleState as VehicleState
from src.vehicle_state import VehicleStateCache as VehicleStateCache
from src.vehicle_state import VehicleStateName as VehicleStateName
from src.vehicle_state import classify_vehicle_state as classify_vehicle_state
from src.vehicle_state import (
    derive_from_latest_rows,
    derive_from_live_source,
    running_replay_id,
)

if TYPE_CHECKING:  # annotation only — src.daemon's package __init__ imports the dashboard
    # Annotation only: importing it at runtime loads the whole src.daemon package (R04.8).
    from src.daemon.health import HealthMonitor
    from src.daemon.inference_metrics import InferenceTimingRecorder

#: Private aliases of the one definition in :mod:`src.vehicle_state` (R03.1).
_FRESHNESS_THRESHOLD_SECONDS = FRESHNESS_THRESHOLD_SECONDS
_RPM_RUNNING_THRESHOLD = RPM_RUNNING_THRESHOLD


@dataclass(frozen=True)
class StatusSnapshot:
    """What ``GET /status`` returns. Stable shape independent of FastAPI."""

    storage_path: str
    free_mb: float
    latest_session: str | None
    latest_session_path: str | None
    model_versions: dict[str, str | None]
    vehicle: str
    storage_warning_mb: float | None = None
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
    #: IMU collector health (H02.5) — :meth:`ImuHealth.snapshot` plus
    #: ``fresh`` — or ``None`` when no IMU is configured.
    imu: dict[str, Any] | None = None
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
    #: CAN transmit gate: ``{enabled, source, suppressed_frames}``. When
    #: ``enabled`` the daemon is receive-only — it still logs broadcast CAN,
    #: stores telemetry and scores anomalies, but issues no OBD polls and
    #: sends no ISO-TP flow control. Surfaced because passive mode looks
    #: exactly like a dead OBD link from every other panel, and an operator
    #: who forgot they armed it would otherwise debug a phantom fault.
    passive: dict[str, Any] | None = None
    #: Focused tuning poll profile: ``{active, source, channels}``. When
    #: active the Mode 01 rotation is restricted and DID windows suspended,
    #: so most channels legitimately stop updating — surfaced for the same
    #: reason as ``passive``, so a narrowed rotation is not mistaken for a
    #: collection fault.
    tuning_mode: dict[str, Any] | None = None
    #: OBD request-rate override (src/collection/bus_budget.py). Surfaced
    #: because an armed override silently changes every channel's achieved
    #: rate, which otherwise reads as a collection anomaly.
    bus_budget: dict[str, Any] | None = None

    #: Operator-selected capture profile. Alias of the generalized runtime
    #: state; ``tuning_mode`` remains for compatibility with older clients.
    capture_profile: dict[str, Any] | None = None
    #: Most recent bounded capture, including why it ended (W03).
    last_capture: dict[str, Any] | None = None
    #: Running calibration: the ECU's own id beside the flash history (W02.2,
    #: W02.3) — :func:`src.control.calibration.calibration_status`. Its keys
    #: are flattened into the payload's top level.
    calibration: dict[str, Any] | None = None
    #: Engine Tuning readiness chips (W02.4), ``{name: {ok, value, detail,
    #: fix}}``. Status only; nothing refuses an action on it.
    tuning_readiness: dict[str, dict[str, Any]] | None = None
    #: S01.1 — seconds from process start to each startup stage, above all
    #: ``first_record_stored`` (marked by the storage sink). ``None`` when no
    #: health monitor is wired.
    startup: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        calibration = self.calibration or {}
        return {
            "storage_path": self.storage_path,
            "free_mb": round(self.free_mb, 1),
            "storage_warning_mb": self.storage_warning_mb,
            "latest_session": self.latest_session,
            "latest_session_path": self.latest_session_path,
            "model_versions": self.model_versions,
            "vehicle": self.vehicle,
            "emitter_counters": self.emitter_counters,
            "health": self.health,
            "vehicle_state": (None if self.vehicle_state is None else self.vehicle_state.to_dict()),
            "gps": self.gps,
            "imu": self.imu,
            "trip": self.trip,
            "profile": self.profile,
            "detectors": self.detectors,
            "model_provenance": self.model_provenance,
            "passive": self.passive,
            "tuning_mode": self.tuning_mode,
            "bus_budget": self.bus_budget,
            "capture_profile": self.capture_profile,
            "last_capture": self.last_capture,
            "ecu_calibration_id": calibration.get("ecu_calibration_id"),
            "ecu_calibration_read_at": calibration.get("ecu_calibration_read_at"),
            "calibration_matches_registry": calibration.get("calibration_matches_registry"),
            "registry_revision": calibration.get("running_revision"),
            "registry_calibration_id": calibration.get("registry_calibration_id"),
            "ecu_calibration_changed": bool(calibration.get("calibration_changed")),
            "calibration_alert": bool(calibration.get("calibration_alert")),
            "calibration_alert_reason": calibration.get("calibration_alert_reason"),
            "tuning_readiness": self.tuning_readiness,
            "startup": self.startup,
        }


def _free_mb(path: Path) -> float:
    """Free disk MB at ``path`` (or its first existing parent). 0 on failure.

    :func:`src.storage.monitor.free_space_mb` does the walk and the division
    (R01.5); ``/status`` must not raise, so a failed ``statvfs`` reads 0.
    """
    try:
        return free_space_mb(path)
    except OSError:
        return 0.0


def _model_version(manifest_path: Path) -> str | None:
    """Read the version field from a model manifest JSON, or ``None``."""
    data = read_json_object(manifest_path)
    if data is None:
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
    data = read_json_object(manifest_path)
    if data is None:
        return None
    return {
        "model_version": data.get("model_version"),
        "created_at": data.get("created_at"),
        "training_data_hash": data.get("training_data_hash"),
        "training_sessions": data.get("training_sessions", []),
        "artifact_format_version": data.get("artifact_format_version"),
    }


def _resolve_latest_session(storage_path: Path) -> Path | None:
    """The session being written right now — captures included.

    Previously this reused ``discover_sessions``, which excludes
    ``_``-prefixed directories so captures stay out of the training corpus.
    Correct for training, wrong here: every live read inherited the exclusion
    and silently showed the *previous* session for the whole duration of a
    capture. Fixing it per-caller would have needed six call sites to
    remember; fixing the resolver makes them right by construction.
    """
    from src.storage.sessions import latest_session

    return latest_session(storage_path)


def _capture_snapshot(storage_writer: Any = None) -> dict[str, Any]:
    """Capture state plus how much this session has recorded so far.

    The row count answers the question a running capture actually raises —
    "is it logging?" — without waiting for a flush or opening the file.
    """
    snapshot = tuning_profile.snapshot()
    if storage_writer is not None:
        snapshot["rows_written"] = storage_writer.rows_written
    return snapshot


def derive_vehicle_state_from_storage(
    storage_path: Path,
    *,
    replay_controller: ReplayController | None,
    now: datetime | None = None,
    session_dir: Path | None = None,
) -> VehicleState:
    """Read the newest session's latest-per-channel rows and classify them.

    The read is here; the classification is
    :func:`src.vehicle_state.derive_from_latest_rows`, the same classifier the
    live and inference derivations use (R03.1). Reads only the newest
    session's Parquet via :func:`read_latest_per_channel`, which is what the
    rest of the dashboard already uses — cheap enough for /status' polling
    cadence. On any I/O failure the rows are empty and the state reads
    ``no_data`` rather than 500-ing — operators routinely hit /status to
    *diagnose* storage issues, so it shouldn't be the thing that breaks first.
    """
    now_dt = datetime.now(UTC) if now is None else now
    # Deferred import: src.dashboard.readers imports _resolve_latest_session
    # from this module, so importing it at module load creates a cycle.
    from src.dashboard.readers import read_latest_per_channel

    try:
        rows = read_latest_per_channel(storage_path, session_dir=session_dir)
    except (OSError, ValueError):
        rows = []
    return derive_from_latest_rows(rows, replay_id=running_replay_id(replay_controller), now=now_dt)


#: Old private names, re-exported (R03.1): the replay guard, autostart and
#: tests import them. The live derivation is
#: :func:`src.vehicle_state.derive_from_live_source`.
_derive_vehicle_state = derive_vehicle_state_from_storage
_derive_vehicle_state_live = derive_from_live_source


def active_session_dir(storage_writer: Any) -> Path | None:
    """The directory the writer is *currently* filling, if it can be read.

    Capture sessions are ``_``-prefixed and invisible to the newest-session
    search on purpose (they must stay out of the training corpus), so during a
    capture the search finds the previous session — which stopped being written
    at capture start. Asking the writer directly is the only way to see live
    state while a capture is running.
    """
    paths = getattr(storage_writer, "paths", None)
    root = getattr(paths, "root", None)
    return root if isinstance(root, Path) else None


_active_session_dir = active_session_dir


def _active_profile(config: CanaryConfig) -> dict[str, str | None] | None:
    """The active vehicle profile ``{slug, name, vin}``.

    The slug is recoverable as the scoped storage path's final component (see
    ``src.vehicle.profiles.scope_config``); name/vin come from the registry, falling
    back to the ``vehicle:`` block's derived label for the default profile that
    may not be registered yet.
    """
    slug = config.collection.storage_path.name
    fallback_name = f"{config.vehicle.year} {config.vehicle.make} {config.vehicle.model}".strip()
    try:
        from src.vehicle.profiles import load_registry

        for profile in load_registry(config.profiles):
            if profile.slug == slug:
                return {"slug": profile.slug, "name": profile.name, "vin": profile.vin}
    except Exception:  # noqa: BLE001 — status must never fail on a bad registry
        return None
    return {"slug": slug, "name": fallback_name, "vin": config.vehicle.vin or None}


#: Per-sink fields the compact view carries — the ones a watchdog acts on.
#: ``queue_depth`` alone hides a sink that filled, dropped and recovered
#: (C10.5), so the high-water mark travels with it.
_COMPACT_SINK_FIELDS: tuple[str, ...] = (
    "queue_depth",
    "queue_high_water",
    "dropped_records",
)

#: Scheduler fields the compact view carries: cumulative counters and the two
#: ratios, never the per-request table or the per-channel rate maps.
_COMPACT_SCHEDULER_FIELDS: tuple[str, ...] = (
    "requests_sent",
    "readings_emitted",
    "request_timeouts",
    "negative_responses",
    "bus_errors",
    "response_rate",
    # D05.1: profile DIDs withheld at a capture start, by reason.
    "profile_start_withheld",
)

#: Inference-clock fields the compact view carries (C08) — deadline health,
#: not stage timing.
_COMPACT_INFERENCE_FIELDS: tuple[str, ...] = (
    "ticks_scheduled",
    "missed_ticks",
    "missed_by_reason",
)

#: ``GET /status?view=`` — ``compact`` is the pollable projection described on
#: :func:`compact_status`. A ``Literal`` so an unrecognised view is a 422 from
#: FastAPI rather than a silent fall-back to the expensive payload.
StatusView = Literal["full", "compact"]


@dataclass(frozen=True)
class StatusSources:
    """The live objects a ``/status`` response is built from, read once.

    Both views are projections of *this*: the compact view narrows these same
    dicts and never re-reads a counter. That matters beyond tidiness — a
    second read a second later is a different number, and telling a watchdog
    (or a frame-loss bracket) that two views of "the same" instant disagree
    is exactly the measurement error this endpoint exists to remove.
    """

    storage_path: Path
    free_mb: float
    vehicle_state: VehicleState
    emitter_counters: dict[str, int] | None
    #: ``HealthMonitor.snapshot()``, or ``None`` when no monitor is wired.
    #: ``None`` means "not instrumented" and never "zero" — a watchdog has to
    #: keep those apart.
    health: dict[str, Any] | None
    #: ``InferenceTimingRecorder.snapshot()`` (the full payload files it under
    #: ``detectors.inference``), or ``None`` when no recorder is wired.
    inference: dict[str, Any] | None
    passive: dict[str, Any]
    capture_profile: dict[str, Any]
    bus_budget: dict[str, Any]
    #: S01.1 — ``StartupTimeline.snapshot()`` (seconds from process start per
    #: startup stage), or ``None`` when no monitor is wired.
    startup: dict[str, Any] | None = None
    #: H02.5 — the IMU collector's counters (:func:`imu_status`), or ``None``
    #: when no IMU is configured. Full view only, like GPS.
    imu: dict[str, Any] | None = None


def imu_status(health: ImuHealth, *, now: float | None = None) -> dict[str, Any]:
    """``/status.imu``: the collector's counters plus ``fresh``.

    ``fresh`` uses :data:`FRESHNESS_THRESHOLD_SECONDS`, the same bound the
    bus freshness behind ``vehicle_state`` uses, so "the IMU is live" and
    "the bus is live" mean the same thing.
    """
    snapshot = health.snapshot(now=datetime.now(UTC).timestamp() if now is None else now)
    age = snapshot["age_seconds"]
    return {"fresh": age is not None and age <= FRESHNESS_THRESHOLD_SECONDS, **snapshot}


def gather_status_sources(
    config: CanaryConfig,
    *,
    emitter: AnomalyEventEmitter | None = None,
    health_monitor: HealthMonitor | None = None,
    replay_controller: ReplayController | None = None,
    storage_writer: Any = None,
    inference_timing: InferenceTimingRecorder | None = None,
    vehicle_state_cache: VehicleStateCache | None = None,
    live_source: Any = None,
    imu_health: ImuHealth | None = None,
) -> StatusSources:
    """Read every source both ``/status`` views need, exactly once.

    ``vehicle_state_cache`` shares the Parquet-backed derivation across
    requests; without one, every call derives fresh (what a test or a
    standalone :func:`build_status` caller wants).
    """
    storage = config.collection.storage_path

    def derive() -> VehicleState:
        if live_source is not None:
            return derive_from_live_source(live_source, replay_controller=replay_controller)
        return derive_vehicle_state_from_storage(
            storage,
            replay_controller=replay_controller,
            session_dir=active_session_dir(storage_writer),
        )

    return StatusSources(
        storage_path=storage,
        free_mb=_free_mb(storage),
        vehicle_state=derive() if vehicle_state_cache is None else vehicle_state_cache.get(derive),
        emitter_counters=emitter.counters.to_dict() if emitter is not None else None,
        health=health_monitor.snapshot() if health_monitor is not None else None,
        inference=inference_timing.snapshot().to_dict() if inference_timing is not None else None,
        passive=tx_gate.snapshot(),
        capture_profile=_capture_snapshot(storage_writer),
        bus_budget=bus_budget_gate.snapshot(),
        startup=health_monitor.startup.snapshot() if health_monitor is not None else None,
        imu=(
            imu_status(imu_health)
            if imu_health is not None and config.hardware.imu.enabled
            else None
        ),
    )


def _pick(source: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    """The named keys of ``source``, absent ones as ``None``."""
    return {name: source.get(name) for name in fields}


def _compact_scheduler(scheduler: dict[str, Any]) -> dict[str, Any]:
    """Scheduler counters without the per-channel/per-request structures.

    ``achieved_hz_60s`` is a map with one entry per polled channel — the bulk
    of the full payload and the thing a poller must not pay for. It is summed
    into ``achieved_hz_60s_total`` (with the channel count as its
    denominator, since a channel dropping out of the plan moves the total on
    its own) and named for what it is: a total of the trailing-window rates,
    not a wire request rate. For the wire rate, difference ``requests_sent``
    between two ``served_at`` stamps.
    """
    rates = scheduler.get("achieved_hz_60s") or {}
    numeric = [value for value in rates.values() if isinstance(value, (int, float))]
    compact = _pick(scheduler, _COMPACT_SCHEDULER_FIELDS)
    compact["achieved_hz_60s_channels"] = len(rates)
    compact["achieved_hz_60s_total"] = round(sum(numeric), 3) if numeric else None
    # Three scalars, and the daemon side of the D01.5 frame-loss bracket
    # (`receive.frames_seen` against the kernel's can0 counters).
    compact["receive"] = scheduler.get("receive")
    return compact


def _compact_health(health: dict[str, Any]) -> dict[str, Any]:
    """``health`` narrowed to the counters a watchdog acts on."""
    scheduler = health.get("obd_scheduler")
    sinks = health.get("telemetry_sinks") or {}
    return {
        "system": health.get("system"),
        "telemetry_sinks": {
            name: _pick(state, _COMPACT_SINK_FIELDS) for name, state in sinks.items()
        },
        "can0": health.get("can0"),
        "obd_scheduler": None if scheduler is None else _compact_scheduler(scheduler),
    }


def compact_status(sources: StatusSources) -> dict[str, Any]:
    """The pollable ``GET /status?view=compact`` payload.

    Purely a projection of ``sources``, so it cannot disagree with the full
    payload built from the same object. Every field sits at the path it
    occupies in the full payload, so a poller that outgrows this view drops
    one query parameter and keeps its accessors. The one key the full payload
    does not also carry verbatim is
    ``health.obd_scheduler.achieved_hz_60s_total``, which is the sum of the
    per-channel map the full payload prints (see :func:`_compact_scheduler`).

    What is left out is left out, not summarised: the per-request table,
    discovery, per-tier and per-channel rate maps, batching, model
    provenance, GPS/trip, and every detector block except the inference
    clock. Nothing here builds a per-channel dict, reads Parquet, or shells
    out — the Parquet-backed ``vehicle_state`` is shared through
    :class:`VehicleStateCache` and reports its own
    ``derived_at``.

    ``served_at`` is stamped beside the counter reads. That is what lets a
    bracketed measurement bound its own window rather than assuming the round
    trip was instant: reading a kernel counter either side of a 1.7 s
    ``/status`` was ±2,700 CAN frames of slack at 1,640 frames/s, as large as
    the loss being measured (D01.5, 2026-09-17).

    A ``None`` block means "not instrumented", never "zero".
    """
    health = sources.health
    return {
        "view": "compact",
        "served_at": datetime.now(UTC).isoformat(),
        "vehicle_state": sources.vehicle_state.to_dict(),
        "free_mb": round(sources.free_mb, 1),
        "emitter_counters": sources.emitter_counters,
        "passive": sources.passive,
        # Name + active only; the profile's channel lists and achieved-rate
        # maps are the per-channel bulk this view exists to skip.
        "capture_profile": {
            "name": sources.capture_profile.get("name"),
            "active": sources.capture_profile.get("active"),
        },
        "health": None if health is None else _compact_health(health),
        # A dozen scalars, and the answer to "when did recording start".
        "startup": sources.startup,
        "detectors": (
            None
            if sources.inference is None
            else {"inference": _pick(sources.inference, _COMPACT_INFERENCE_FIELDS)}
        ),
    }


def build_status(
    config: CanaryConfig,
    *,
    models_dir: Path | None = None,
    emitter: AnomalyEventEmitter | None = None,
    health_monitor: HealthMonitor | None = None,
    replay_controller: ReplayController | None = None,
    gps_tracker: GpsTracker | None = None,
    storage_writer: Any = None,
    inference_timing: InferenceTimingRecorder | None = None,
    sources: StatusSources | None = None,
    ups_status_path: Path = DEFAULT_UPS_STATUS_PATH,
    dids_path: Path | None = None,
) -> StatusSnapshot:
    """Assemble the status snapshot used by ``GET /status``.

    ``inference_timing`` (C08) folds the live-inference scheduler's stage
    timing, loop lateness and missed-deadline counters into
    ``detectors.inference`` — kept apart from ``health.inference_latency_ms``
    (model scoring latency only) so clock health and model cost are never
    read as one number.

    ``sources`` is a pre-gathered :class:`StatusSources`; pass the same one to
    :func:`compact_status` to serve both views of a single instant. Gathered
    here when omitted, from the keyword arguments above.
    """
    if sources is None:
        sources = gather_status_sources(
            config,
            emitter=emitter,
            health_monitor=health_monitor,
            replay_controller=replay_controller,
            storage_writer=storage_writer,
            inference_timing=inference_timing,
        )
    storage = sources.storage_path
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
                # A04.6/A04.8 — mode models refused at load and trained
                # features the feature policy forbids, named.
                "load_audit": load_audit_snapshot().get("isolation_forest"),
            },
            # D02.5 — `blind_modes` says a detector is scoring nothing;
            # this says *why*, when the reason is that its feature spec
            # cannot be satisfied by the live pipeline at all (a channel
            # renamed or retracted out of `data/bindings.yaml` since the
            # artifact was trained). Empty when every spec is satisfiable.
            "structural_blindness": structural_blindness_snapshot(),
            # 4.7 — complements the load-time unknown_operating_modes warning:
            # this is the *runtime* version, when the live segmenter can't
            # run at all (its input columns are missing) rather than being
            # configured for a mode set that doesn't match.
            "segmenter": {
                "fallback_active": health_monitor.segmenter_fallback_active,
            },
            # R03.12 — IF and sequence score only under a corpus-eligible
            # profile. ``scoring_allowed: false`` names the profile (and
            # whether it came from the live daemon or a replayed manifest)
            # that is withholding them; ``emitter_counters.
            # skipped_capture_profile`` counts the withheld ticks.
            "capture_profile_gate": health_monitor.capture_profile_gate,
        }
    if sources.inference is not None:
        detectors = {**(detectors or {}), "inference": sources.inference}
    calibration = _calibration_status(config)
    tuning_readiness = build_tuning_readiness(
        power=build_power_snapshot(
            read_watchdog_status(ups_status_path), read_keepalive_remaining()
        ),
        ups_floor_percent=config.tuning_readiness.min_ups_soc_percent,
        free_mb=sources.free_mb,
        storage_floor_mb=float(config.collection.storage_warning_mb),
        memory_summary=_memory_verify_summary(config, dids_path),
        calibration=calibration,
        vehicle_state=sources.vehicle_state.state,
    )
    gps_on = config.hardware.gps.enabled
    return StatusSnapshot(
        storage_path=str(storage),
        free_mb=sources.free_mb,
        storage_warning_mb=config.collection.storage_warning_mb,
        latest_session=latest.name if latest else None,
        latest_session_path=str(latest) if latest else None,
        model_versions=model_versions,
        vehicle=f"{config.vehicle.year} {config.vehicle.make} {config.vehicle.model}".strip(),
        emitter_counters=sources.emitter_counters,
        health=sources.health,
        vehicle_state=sources.vehicle_state,
        # D03.29: the tracker exists on a GPS-less install too (it ends trips
        # from vehicle speed), but its fix snapshot and GPS-integrated
        # odometer mean nothing there — report them as absent, not as 0 km.
        gps=gps_tracker.gps_status() if gps_tracker is not None and gps_on else None,
        imu=sources.imu,
        trip=gps_tracker.trip_status() if gps_tracker is not None and gps_on else None,
        profile=_active_profile(config),
        detectors=detectors,
        model_provenance=model_provenance if models_dir is not None else None,
        passive=sources.passive,
        tuning_mode=tuning_profile.snapshot(),
        bus_budget=sources.bus_budget,
        capture_profile=sources.capture_profile,
        last_capture=latest_capture_summary(storage),
        calibration=calibration,
        tuning_readiness=tuning_readiness,
        startup=sources.startup,
    )


def _calibration_status(config: CanaryConfig) -> dict[str, Any]:
    """:func:`calibration_status`, degraded rather than raised on a bad registry.

    A typo in ``tune_revisions.yaml`` must not take ``/status`` down with it;
    the ECU side is still reported, the comparison is unknown, and the alert
    says why.
    """
    from src.control.calibration import calibration_status

    try:
        return calibration_status(config)
    except Exception as exc:  # noqa: BLE001 - a status read must not fail on it
        ecu = tuning_profile.calibration_snapshot()
        return {
            "running_revision": None,
            "registry_calibration_id": None,
            "ecu_calibration_id": ecu["ecu_calibration_id"],
            "ecu_calibration_read_at": ecu["ecu_calibration_read_at"],
            "calibration_matches_registry": None,
            "calibration_changed": bool(ecu["ecu_calibration_changed"]),
            "calibration_alert": True,
            "calibration_alert_reason": f"flash history unreadable: {exc}",
        }


def _memory_verify_summary(config: CanaryConfig, dids_path: Path | None) -> dict[str, int] | None:
    if dids_path is None:
        return None
    from src.control.calibration import memory_verify_summary

    try:
        return memory_verify_summary(config, dids_path)
    except Exception:  # noqa: BLE001 - reported as "not readable", never a 500
        return None


__all__ = [
    "VEHICLE_STATE_TTL_SECONDS",
    "StatusSnapshot",
    "StatusSources",
    "StatusView",
    "VehicleState",
    "VehicleStateCache",
    "VehicleStateName",
    "build_status",
    "classify_vehicle_state",
    "compact_status",
    "gather_status_sources",
]
