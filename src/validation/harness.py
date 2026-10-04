"""Run one C10 scenario through the real daemon path (C10.1/C10.2).

Real clock, real threads: the Canable collector thread
(``collect_can_obd_from_config`` → ``run_can_obd_loop``) polls a paced
:class:`~src.validation.synthetic_bus.SyntheticEcu`; its readings and every
tapped frame cross the ``ThreadHandoff`` into the ingest queue; the daemon
fanout (``drain_queue_with_broadcast``) feeds the real Parquet writer, the
dashboard broadcaster, the live feature source, the DTC and monitor sinks;
``run_live_inference`` ticks the detectors on its deadline grid; and the
capture is stopped through ``capture_finalize.finalize_capture`` exactly as
``POST /control/capture/stop`` does. What is *not* real is declared in every
report: the adapter, the ECU, the model artifacts and the kernel counters.

The compromise this design makes: timing numbers are workstation numbers
with scheduler jitter, and the ECU's latency is a sleep. What it buys is that
the handoff, the fanout, the writer and the finalize order are the shipped
code paths, not re-implementations.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import logging
import resource
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import can

from src.atomic_write import atomic_write_text
from src.collection import (
    bus_budget,
    capture_generation,
    capture_lifetime,
    discovery,
    tuning_profile,
    tx_gate,
)
from src.collection.can_collector import CanReceiveStats, collect_can_from_config
from src.collection.can_obd_collector import collect_can_obd_from_config
from src.collection.capture_lifetime import InterarrivalReservoir
from src.collection.capture_profiles import CaptureProfile
from src.collection.gps_collector import reading_to_records
from src.collection.gps_nmea import GpsReading
from src.collection.obd_scheduler import (
    CanObdSpec,
    ObdSchedulerMetrics,
    known_did_names,
    parse_did_token,
    resolve_did_session_spec,
)
from src.collection.queueing import CollectorHandoffStats
from src.collection.reconnect import ReconnectPolicy
from src.collection.replay import ReplayEmissionStats, replay_session
from src.config import CanaryConfig, rebase_state_paths
from src.control.capture_finalize import StartEpoch, finalize_capture, record_capture_start
from src.daemon.broadcast import Broadcaster, Subscription
from src.daemon.components import DaemonComponents, build_components
from src.daemon.drain import drain_queue_with_broadcast
from src.daemon.inference import build_live_feature_source, run_live_inference
from src.daemon.ingress import LiveIngress
from src.daemon.runtime import (
    can_receive_stats_observer,
    drain_kwargs,
    obd_handoff_stats_observer,
)
from src.features.live import LiveFeatureSource
from src.models.manager import LoadedModel, default_manifest_sidecar
from src.models.manifest import ModelManifestCommon
from src.paths import BINDINGS_YAML
from src.storage.async_writer import AsyncParquetTelemetryWriter, ensure_async_writer
from src.storage.parquet_writer import ParquetTelemetryWriter
from src.telemetry import TelemetryRecord
from src.validation.scenarios import (
    ALL_DIDS,
    MODE01_CHANNELS,
    ScenarioSpec,
    StorageStall,
    TimelineAction,
    build_profile,
    build_spec_kwargs,
)
from src.validation.synthetic_bus import (
    BroadcastLedger,
    RealClock,
    SyntheticEcu,
    mode01_support_map,
    write_synthetic_bindings,
)
from src.vehicle.profiles import resolve_profile_for_tooling, scope_config

log = logging.getLogger(__name__)

REAL_BINDINGS = BINDINGS_YAML
DRIVE_SLICE = Path("tests/fixtures/ground_truth/drive-slice")
HEARTBEAT_INTERVAL_S = 0.01
RECONNECT_POLICY = ReconnectPolicy(initial_delay_seconds=0.5, max_delay_seconds=1.0)
#: Daemon paths the harness reads from the real tree: detector artifacts and
#: the per-vehicle inputs. Every other ``Path`` field of ``DaemonConfig`` --
#: including any added later -- is rebased under the scratch dir by
#: :func:`src.config.rebase_state_paths` (R07.2).
_REAL_TREE_DAEMON_INPUTS: tuple[str, ...] = (
    "models_dir",
    "behavior_models",
    "accepted_baselines",
    "custom_pids",
    "afr_calibration",
    "kline_profile",
)


# ---------------------------------------------------------------------------
# bounded instrumentation
# ---------------------------------------------------------------------------


class BoundedSamples:
    """A reservoir plus running count/max: O(1) memory for any run length."""

    def __init__(self, capacity: int = 2048) -> None:
        self._reservoir = InterarrivalReservoir(capacity)
        self.count = 0
        self.max = 0.0
        self.last = 0.0

    def add(self, value: float) -> None:
        self.count += 1
        self.max = max(self.max, value)
        self.last = value
        self._reservoir.add(value)

    def summary_ms(self) -> dict[str, Any]:
        def q(p: float) -> float | None:
            value = self._reservoir.quantile(p)
            return None if value is None else round(value * 1000.0, 3)

        return {
            "count": self.count,
            "median_ms": q(0.5),
            "p95_ms": q(0.95),
            "p99_ms": q(0.99),
            "max_ms": round(self.max * 1000.0, 3) if self.count else None,
            "reservoir_capacity": self._reservoir.capacity,
        }


class InstrumentedParquetWriter(ParquetTelemetryWriter):
    """The real writer plus flush timing and an optional scripted stall."""

    def __init__(self, out_root: Path, *, stall: StorageStall | None = None, **kwargs: Any) -> None:
        super().__init__(out_root, **kwargs)
        self.flush_samples = BoundedSamples()
        self.stalls = 0
        self._stall = stall
        self._origin = time.monotonic()

    def flush(self) -> Path | None:
        started = time.perf_counter()
        if self._stall is not None:
            elapsed = time.monotonic() - self._origin
            if self._stall.start_s <= elapsed < self._stall.end_s:
                self.stalls += 1
                time.sleep(self._stall.sleep_s)
        path = super().flush()
        if path is not None:
            self.flush_samples.add(time.perf_counter() - started)
        return path


class _IdentityScaler:
    def transform(self, frame: Any) -> Any:
        return frame


@dataclass
class StubIsolationForest:
    """A declared stand-in for the missing IF artifact: exercises the
    worker compute path with a constant, never-anomalous score."""

    feature_columns: tuple[str, ...] = ("obd:RPM", "obd:SPEED", "obd:ENGINE_LOAD")
    mode_column: str = "operating_mode"
    manifest: Any = field(default_factory=lambda: SimpleNamespace(model_version="c10-stub"))
    scaler: Any = field(default_factory=_IdentityScaler)
    score_scales: dict[str, float] = field(default_factory=dict)
    calls: int = 0

    def score(self, frame: Any) -> Any:
        self.calls += 1
        out = frame.copy()
        out["isolation_forest_score"] = 0.05
        out["isolation_forest_label"] = 1
        out["isolation_forest_model_mode"] = (
            out[self.mode_column] if self.mode_column in out else "unknown"
        )
        out["isolation_forest_missing_features"] = False
        out["isolation_forest_calibrated_threshold"] = 0.5
        out["isolation_forest_calibrated_anomaly"] = False
        return out


class StubModelManager:
    def __init__(self, artifact: StubIsolationForest) -> None:
        self.artifact = artifact

    @property
    def current(self) -> LoadedModel[Any]:
        return LoadedModel(
            artifact=self.artifact,
            manifest=ModelManifestCommon(
                artifact_format_version=1,
                model_type="isolation_forest",
                model_version="c10-stub",
                created_at=datetime.now(UTC).isoformat(),
                training_data_hash="synthetic",
                input_rows=0,
            ),
            artifact_path=Path("/nonexistent/c10-stub"),
            manifest_path=Path("/nonexistent/c10-stub.manifest.json"),
            manifest_mtime=0.0,
        )

    def poll(self) -> None:
        return None


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def harness_config(
    scratch: Path,
    *,
    base: Path = Path("canary.example.yaml"),
    models_enabled: bool = True,
    models_dir: Path | None = None,
) -> CanaryConfig:
    """The example daemon config, profile-scoped like the daemon, with every
    control/state path redirected under ``scratch``.

    ``models_dir`` overrides where detector artifacts are looked up (it is
    profile-scoped afterwards, exactly as the daemon scopes it). Only
    manifests are committed under ``models/``, so a workstation run installs
    the declared IF stub by default; point this at a directory holding a real
    ``model.joblib``/``model.pt`` to measure per-detector occupancy against
    real artifacts (C08.1). On the Pi the deployed ``models_dir`` already
    holds them and needs no override.
    """
    config = CanaryConfig.from_yaml(base)
    if models_dir is not None:
        config = config.model_copy(
            update={"daemon": config.daemon.model_copy(update={"models_dir": models_dir})}
        )
    config = scope_config(config, resolve_profile_for_tooling(config, None))
    daemon = rebase_state_paths(config, scratch, keep=_REAL_TREE_DAEMON_INPUTS).daemon
    collection = config.collection.model_copy(
        update={"storage_path": scratch / "storage", "replay_session": None}
    )
    can_cfg = config.hardware.can.model_copy(
        update={"interface": "virtual", "channel": "c10-synthetic"}
    )
    hardware = config.hardware.model_copy(update={"can": can_cfg})
    models = config.models
    if not models_enabled:
        models = models.model_copy(
            update={
                "isolation_forest": models.isolation_forest.model_copy(update={"enabled": False}),
                "lstm_autoencoder": models.lstm_autoencoder.model_copy(update={"enabled": False}),
            }
        )
    return config.model_copy(
        update={"daemon": daemon, "collection": collection, "hardware": hardware, "models": models}
    )


def reset_process_state(default_profile: CaptureProfile) -> None:
    """The process-global gates every scenario must start from."""
    tuning_profile.configure_default(default_profile)
    aliases = {name: f"DID_{parse_did_token(name):04X}" for name in known_did_names()}
    tuning_profile.configure_did_aliases(aliases)
    tx_gate.reset_ecm_session_claims()
    if tx_gate.is_passive():
        tx_gate.set_passive(False, source="validation")
    if bus_budget.override() is not None:
        bus_budget.set_override(None, source="validation", ttl_seconds=None)
    discovery.reset_discovery()
    capture_lifetime.end()


def resolved_plan_for(config: CanaryConfig, profile: CaptureProfile) -> dict[str, Any] | None:
    """The identifier-level plan the control route writes into the capture
    manifest (C09); ``None`` on a tree that predates it."""
    from src.collection.capture_profiles import CaptureProfileSources, CaptureProfileStore

    store = CaptureProfileStore(
        CaptureProfileSources.of(config.daemon), config.collection, config.daemon.behavior_models
    )
    builder = getattr(store, "resolved_plan", None)
    if builder is None:
        return None
    try:
        plan: dict[str, Any] = builder(profile)
    except Exception as exc:  # noqa: BLE001 — reported, never fatal
        log.warning("resolved plan unavailable for %s: %s", profile.name, exc)
        return None
    return plan


def arm_profile(profile: CaptureProfile, *, session_dir: Path, source: str = "validation") -> None:
    """What ``POST /control/capture/start`` does after rotating the writer:
    the profile's budget override, then the activation (that order)."""
    if profile.bus_budget_override is not None:
        bus_budget.set_override(
            profile.bus_budget_override, source=f"capture:{profile.name}", ttl_seconds=None
        )
    tuning_profile.activate(profile, session_dir=str(session_dir), source=source)


def install_models(components: DaemonComponents, config: CanaryConfig) -> dict[str, Any]:
    """Real artifacts when present; a declared stub for the IF otherwise."""
    status: dict[str, Any] = {}
    for name, manager in list(components.model_managers.items()):
        loaded = manager.current
        if loaded is not None:
            status[name] = {"status": "artifact", "version": loaded.serving_version}
            continue
        relative = (
            config.daemon.isolation_forest_artifact
            if name == "isolation_forest"
            else config.daemon.lstm_artifact
        )
        artifact_path = config.daemon.models_dir / relative
        manifest_path = default_manifest_sidecar(artifact_path)
        if name == "isolation_forest":
            stub = StubIsolationForest()
            components.model_managers[name] = StubModelManager(stub)  # type: ignore[assignment]
            status[name] = {
                "status": "stub",
                "reason": f"artifact missing at {artifact_path} "
                f"(manifest present: {manifest_path.exists()}); constant-score stub installed",
                "version": "c10-stub",
            }
        else:
            status[name] = {
                "status": "absent",
                "reason": f"artifact missing at {artifact_path} "
                f"(manifest present: {manifest_path.exists()}); sequence detector not exercised",
            }
    if "isolation_forest" not in status:
        status["isolation_forest"] = {
            "status": "disabled",
            "reason": "models.isolation_forest.enabled false",
        }
    return status


def build_can_spec(config: CanaryConfig, spec: ScenarioSpec) -> tuple[CanObdSpec, list[str]]:
    collection = config.collection.model_copy(update={"obd_dids": ALL_DIDS})
    mapping: dict[str, Any] = {
        "can": config.hardware.can,
        "poll_rate_hz": 1.0,
        "pids": MODE01_CHANNELS,
        "dids": ALL_DIDS,
        "capture_profiles_enabled": True,
        "batch_max_pids": 1,
        "did_batch_max_dids": 1,
        "did_request_id": collection.obd_did_request_id,
        "request_timeout_seconds": 0.25,
        "did_session": resolve_did_session_spec(collection),
        "dtc_poll_interval_seconds": 0.0,
        "service_poll_interval_seconds": 0.0,
        "discovery_key": None,
    }
    widths = getattr(_scheduler_module(), "did_response_widths", None)
    if widths is not None:
        did_widths, did_sources = widths()
        mapping["did_response_widths"] = did_widths
        mapping["did_response_width_sources"] = did_sources
    mapping.update(spec.spec_overrides)
    kwargs, dropped = build_spec_kwargs(mapping)
    return CanObdSpec(**kwargs), dropped


def _scheduler_module() -> Any:
    import src.collection.obd_scheduler as module

    return module


def write_default_manifest(
    session_dir: Path, profile: CaptureProfile, *, plan: dict[str, Any] | None
) -> None:
    """The sidecar ``enter_capture_mode`` writes, for a normal-collection run."""
    sidecar = {
        "schema_version": 2,
        "profile": profile.name,
        "started_at": datetime.now(UTC).isoformat(),
        "tune_revision": None,
        "profile_snapshot": profile.model_dump(mode="json"),
        "resolved_polling_plan": plan,
    }
    atomic_write_text(
        session_dir / "capture_profile.json", json.dumps(sidecar, indent=2, sort_keys=True) + "\n"
    )


# ---------------------------------------------------------------------------
# run artifacts
# ---------------------------------------------------------------------------


@dataclass
class RunArtifacts:
    """Everything one scenario run produced, before analysis."""

    scenario: ScenarioSpec
    label: str
    config: CanaryConfig
    session_dirs: list[Path]
    capture_dir: Path | None
    profile: CaptureProfile
    profile_fields_dropped: list[str]
    spec_fields_dropped: list[str]
    can_spec: CanObdSpec | None
    bindings_path: Path
    models: dict[str, Any]
    components_enabled: dict[str, Any]
    started_wall: float = 0.0
    armed_wall: float = 0.0
    armed_monotonic: float = 0.0
    ended_wall: float = 0.0
    buses: list[SyntheticEcu] = field(default_factory=list)
    ledger: BroadcastLedger | None = None
    scheduler_metrics: dict[str, Any] | None = None
    #: The last snapshot published before ``finalize_capture`` bumped the
    #: generation (which resets per-generation cycle accounting).
    scheduler_metrics_before_stop: dict[str, Any] | None = None
    scheduler_snapshots: int = 0
    handoff: dict[str, Any] | None = None
    receive_stats: dict[str, Any] | None = None
    finalize: dict[str, Any] | None = None
    start_epoch: StartEpoch | None = None
    stop_epoch: dict[str, Any] | None = None
    health_snapshot: dict[str, Any] = field(default_factory=dict)
    inference: dict[str, Any] | None = None
    emitter_counters: dict[str, Any] = field(default_factory=dict)
    heartbeat: dict[str, Any] = field(default_factory=dict)
    rusage: dict[str, Any] = field(default_factory=dict)
    dashboard: dict[str, Any] = field(default_factory=dict)
    scores: dict[str, int] = field(default_factory=dict)
    writer_flush: dict[str, Any] = field(default_factory=dict)
    writer_stalls: int = 0
    rows_written: int = 0
    #: CAN frames the writer's change filter did not store (P02.10), over
    #: every session of the run: part of the push identity, never loss.
    can_frames_suppressed: int = 0
    generation_events: list[dict[str, Any]] = field(default_factory=list)
    reconnect_attempts: int = 0
    if_stub_calls: int | None = None
    replay_source: Path | None = None
    replay_speed: float | None = None
    #: What the replay emit path actually read/emitted/skipped (D02.9). The
    #: harness is the off-vehicle instrument for replay row accounting, so the
    #: counters land in the report beside the source it replayed.
    replay_emission: ReplayEmissionStats | None = None
    #: Rows a simulated live GPS receiver pushed onto the queue during the
    #: replay (``ScenarioSpec.live_gps_hz``); 0 when none ran.
    live_gps_rows_pushed: int = 0
    #: Whether this tree could write the C09 resolved polling plan into the
    #: manifest (the recompute path takes requested rates from it).
    resolved_plan_written: bool = False
    #: Host wall-clock steps observed during the run (see ``_heartbeat``).
    wall_clock_steps: list[dict[str, float]] = field(default_factory=list)
    attempts_used: int = 1

    @property
    def clock_stepped(self) -> bool:
        return bool(self.wall_clock_steps)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


#: A wall-clock jump larger than this between two heartbeats is a *step*
#: (NTP/hypervisor correction), not drift. Measured on the WSL2 workstation:
#: ~0.9 s backward steps every couple of minutes, which re-orders every
#: wall-stamped row taken across the step. A run containing one is rerun.
WALL_CLOCK_STEP_THRESHOLD_S = 0.02
MAX_RECORDED_STEPS = 32


async def _heartbeat(
    stop: asyncio.Event, samples: BoundedSamples, clock_steps: list[dict[str, float]]
) -> None:
    loop = asyncio.get_running_loop()
    last_offset = time.time() - time.monotonic()
    while not stop.is_set():
        before = loop.time()
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)
        samples.add(max(0.0, loop.time() - before - HEARTBEAT_INTERVAL_S))
        offset = time.time() - time.monotonic()
        if abs(offset - last_offset) > WALL_CLOCK_STEP_THRESHOLD_S:
            if len(clock_steps) < MAX_RECORDED_STEPS:
                clock_steps.append(
                    {"at_monotonic": time.monotonic(), "step_s": round(offset - last_offset, 4)}
                )
            last_offset = offset


async def _consume(
    sub: Subscription, stop: asyncio.Event, on_message: Callable[[Any], None]
) -> None:
    while not stop.is_set():
        try:
            message = await asyncio.wait_for(sub.queue.get(), timeout=0.2)
        except TimeoutError:
            continue
        on_message(message)


#: What the simulated live receiver reports: a parked fix at a position no
#: recorded drive contains, so a contaminating row is identifiable by value.
LIVE_GPS_SENTINEL = GpsReading(
    has_fix=True,
    speed_kmh=0.0,
    heading_deg=0.0,
    latitude=0.0,
    longitude=0.0,
    altitude_m=0.0,
    fix_quality=1,
    num_satellites=9,
    hdop=0.9,
)


async def _live_gps(
    queue: asyncio.Queue[TelemetryRecord],
    stop: asyncio.Event,
    hz: float,
    artifacts: RunArtifacts,
) -> None:
    """A live GPS receiver running beside the replay (D02.9 item 3).

    On the Pi the receiver keeps producing when a replay starts; before
    D02.9 its rows landed in whatever session the writer pointed at — the
    ``_replay-*`` directory. The daemon now hands every live collector a
    :class:`~src.daemon.ingress.LiveIngress` that ``ReplayController`` closes
    for the replay, and the caller passes the same closed ingress here. This
    pushes the same ``reading_to_records`` rows the real collector does for as
    long as the replay runs; the count lands in
    ``artifacts.live_gps_rows_pushed``.
    """
    period = 1.0 / hz
    while not stop.is_set():
        for record in reading_to_records(LIVE_GPS_SENTINEL, time.time()):
            try:
                queue.put_nowait(record)
                artifacts.live_gps_rows_pushed += 1
            except asyncio.QueueFull:
                pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=period)
        except TimeoutError:
            pass


async def _timeline(
    actions: tuple[TimelineAction, ...],
    armed: float,
    artifacts: RunArtifacts,
    writer: InstrumentedParquetWriter,
) -> None:
    loop = asyncio.get_running_loop()
    for action in sorted(actions, key=lambda a: a.at_s):
        delay = armed + action.at_s - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        if action.action == "pause":
            tuning_profile.set_paused(True)
        elif action.action == "resume":
            tuning_profile.set_paused(False)
        lifetime_before: dict[str, Any] | None = None
        if action.action == "activate" and action.profile is not None:
            # ``activate`` restarts the capture ledger: keep the phase that ends
            # here (its pause windows, its per-channel coverage) as evidence.
            snapshot = capture_lifetime.snapshot(time.monotonic())
            lifetime_before = None if snapshot is None else snapshot.to_dict()
            profile, _dropped = build_profile(action.profile)
            arm_profile(profile, session_dir=writer.paths.root)
        artifacts.generation_events.append(
            {
                "at_s": round(loop.time() - armed, 3),
                "action": action.action,
                "profile": None if action.profile is None else action.profile["name"],
                "generation": capture_generation.current(),
                "lifetime_before": lifetime_before,
            }
        )


def _rusage() -> dict[str, float]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "utime_s": usage.ru_utime,
        "stime_s": usage.ru_stime,
        "maxrss_kb": float(usage.ru_maxrss),
    }


def _rusage_delta(
    before: dict[str, float], after: dict[str, float], wall_s: float
) -> dict[str, Any]:
    cpu = (after["utime_s"] - before["utime_s"]) + (after["stime_s"] - before["stime_s"])
    return {
        "source": "resource.getrusage(RUSAGE_SELF)",
        "user_cpu_s": round(after["utime_s"] - before["utime_s"], 3),
        "system_cpu_s": round(after["stime_s"] - before["stime_s"], 3),
        "cpu_percent_of_one_core": round(100.0 * cpu / wall_s, 2) if wall_s > 0 else None,
        "max_rss_mb": round(after["maxrss_kb"] / 1024.0, 1),
        "note": "process-wide, includes the harness itself; the Pi figure is a C10.10 measurement",
    }


def _counters_dict(counters: Any) -> dict[str, Any]:
    if dataclasses.is_dataclass(counters) and not isinstance(counters, type):
        return {k: v for k, v in dataclasses.asdict(counters).items()}
    return dict(getattr(counters, "__dict__", {}))


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


@dataclass
class _Pipeline:
    """The running daemon half of a scenario: sinks, inference and the
    dashboard/score counters, as :func:`_start_pipeline` wired them."""

    components: DaemonComponents
    writer: InstrumentedParquetWriter
    async_writer: AsyncParquetTelemetryWriter
    live_source: LiveFeatureSource | None
    queue: asyncio.Queue[TelemetryRecord]
    drain_stop: asyncio.Event
    collector_stop: asyncio.Event
    inference_stop: asyncio.Event
    aux_stop: asyncio.Event
    drain_task: asyncio.Task[None]
    inference_task: asyncio.Task[None] | None
    aux_tasks: list[asyncio.Task[None]]
    telemetry_sub: Subscription
    scores_sub: Subscription
    heartbeat: BoundedSamples
    clock_steps: list[dict[str, float]]
    dashboard_count: list[int] = field(default_factory=lambda: [0])
    scores: dict[str, int] = field(default_factory=dict)


def _start_pipeline(
    spec: ScenarioSpec,
    *,
    label: str,
    sessions_root: Path,
    components: DaemonComponents,
    config: CanaryConfig,
    bindings_path: Path,
    enable_inference: bool,
) -> _Pipeline:
    """Writer, drain fanout, dashboard/score consumers, heartbeat and the
    inference loop — the daemon's drain wiring composed from
    :func:`src.daemon.runtime.drain_kwargs` (R04.13), not a copy of it."""
    writer = InstrumentedParquetWriter(
        sessions_root / label / spec.name,
        stall=spec.storage_stall,
        flush_interval_seconds=2.0,
        max_rows=20_000,
        broadcast_stale_timeout_seconds=config.collection.broadcast_stale_timeout_seconds,
    )
    async_writer = ensure_async_writer(writer)
    components.storage_writer = async_writer
    live_source = (
        build_live_feature_source(components, bindings_path=bindings_path)
        if enable_inference
        else None
    )
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue(maxsize=spec.queue_max_size)
    drain_stop = asyncio.Event()
    inference_stop = asyncio.Event()
    aux_stop = asyncio.Event()
    drain_task = asyncio.create_task(
        drain_queue_with_broadcast(
            queue,
            async_writer,
            drain_stop,
            live_source=live_source,
            poll_interval=0.1,
            # The daemon's wiring; the trip hooks stay unset because a trip
            # end would schedule a whole-session digest mid-measurement.
            **drain_kwargs(
                config,
                components,
                active_session_path=lambda: writer.paths.root,
            ),
        ),
        name="c10-drain",
    )
    telemetry_sub = components.telemetry_broadcaster.subscribe()
    scores_sub = components.scores_broadcaster.subscribe()
    heartbeat = BoundedSamples()
    clock_steps: list[dict[str, float]] = []
    pipeline = _Pipeline(
        components=components,
        writer=writer,
        async_writer=async_writer,
        live_source=live_source,
        queue=queue,
        drain_stop=drain_stop,
        collector_stop=asyncio.Event(),
        inference_stop=inference_stop,
        aux_stop=aux_stop,
        drain_task=drain_task,
        inference_task=None,
        aux_tasks=[],
        telemetry_sub=telemetry_sub,
        scores_sub=scores_sub,
        heartbeat=heartbeat,
        clock_steps=clock_steps,
    )

    def _count_dashboard(_message: Any) -> None:
        pipeline.dashboard_count[0] += 1

    def _count_score(message: Any) -> None:
        source = str(message.get("source", "?")) if isinstance(message, Mapping) else "?"
        pipeline.scores[source] = pipeline.scores.get(source, 0) + 1

    pipeline.aux_tasks.extend(
        [
            asyncio.create_task(_consume(telemetry_sub, aux_stop, _count_dashboard)),
            asyncio.create_task(_consume(scores_sub, aux_stop, _count_score)),
            asyncio.create_task(_heartbeat(aux_stop, heartbeat, clock_steps)),
        ]
    )
    if live_source is not None:
        pipeline.inference_task = asyncio.create_task(
            run_live_inference(
                live_source, components, inference_stop, tick_seconds=spec.inference_tick_s
            ),
            name="c10-inference",
        )
    return pipeline


async def _start_collector(
    spec: ScenarioSpec,
    *,
    label: str,
    config: CanaryConfig,
    can_spec: CanObdSpec | None,
    pipeline: _Pipeline,
    artifacts: RunArtifacts,
) -> asyncio.Task[Any]:
    """Start the scenario's collector (Canable, passive or replay) on the
    pipeline's queue, observed through the daemon's own health observers."""
    health = pipeline.components.health_monitor
    queue, collector_stop = pipeline.queue, pipeline.collector_stop
    ledger = BroadcastLedger(origin_monotonic=time.monotonic())
    artifacts.ledger = ledger
    ecu_kwargs = dict(spec.ecu)
    if ecu_kwargs.pop("supports_mode01_map", False):
        ecu_kwargs["mode01_support"] = mode01_support_map()

    def factory(_cfg: Any) -> can.BusABC:
        bus = SyntheticEcu(
            RealClock(),
            ledger=ledger,
            broadcast=spec.broadcast,
            fault_at_elapsed_s=spec.fault_at_elapsed_s if not artifacts.buses else None,
            **ecu_kwargs,
        )
        artifacts.buses.append(bus)
        artifacts.reconnect_attempts = len(artifacts.buses) - 1
        return bus  # type: ignore[return-value]

    def on_metrics(metrics: ObdSchedulerMetrics) -> None:
        health.observe_obd_scheduler(metrics)
        artifacts.scheduler_metrics = metrics.to_dict()
        artifacts.scheduler_snapshots += 1

    observe_handoff = obd_handoff_stats_observer(health)

    def on_handoff(stats: CollectorHandoffStats) -> None:
        observe_handoff(stats)
        artifacts.handoff = stats.to_dict()

    observe_receive = can_receive_stats_observer(health)

    def on_receive(stats: CanReceiveStats) -> None:
        observe_receive(stats)
        artifacts.receive_stats = {
            "frames": stats.frames,
            "parse_errors": stats.parse_errors,
            "queue_dropped": stats.queue_dropped,
            "buffer_dropped": stats.handoff.dropped,
            "buffer_high_water": stats.handoff.high_water,
            "buffer_capacity": stats.handoff.capacity,
            "batches": stats.handoff.drains,
            "reconnects": stats.reconnects,
        }

    artifacts.started_wall = time.time()
    if spec.kind == "canable":
        assert can_spec is not None
        return asyncio.create_task(
            collect_can_obd_from_config(
                can_spec,
                queue,
                collector_stop,
                factory=factory,
                reconnect_policy=RECONNECT_POLICY,
                on_scheduler_metrics=on_metrics,
                on_handoff_stats=on_handoff,
            ),
            name="c10-collector",
        )
    if spec.kind == "passive":
        return asyncio.create_task(
            collect_can_from_config(
                config.hardware.can,
                queue,
                collector_stop,
                poll_interval=0.05,
                factory=factory,
                reconnect_policy=RECONNECT_POLICY,
                on_receive_stats=on_receive,
            ),
            name="c10-passive",
        )
    replay_source = Path(spec.replay_source) if spec.replay_source else DRIVE_SLICE
    artifacts.replay_source = replay_source
    artifacts.replay_speed = spec.replay_speed
    replay_stats = ReplayEmissionStats()
    artifacts.replay_emission = replay_stats
    await pipeline.async_writer.enter_replay_mode(f"validation-{label}-{int(time.time())}")
    collector_task = asyncio.create_task(
        replay_session(
            queue,
            replay_source,
            collector_stop,
            speed=spec.replay_speed,
            loop=False,
            stats=replay_stats,
            broadcast_stale_timeout_seconds=config.collection.broadcast_stale_timeout_seconds,
        ),
        name="c10-replay",
    )
    if spec.live_gps_hz > 0:
        # The daemon's door for live collectors, closed for the replay exactly
        # as ``ReplayController.start`` closes it (D02.9).
        live_ingress = LiveIngress(queue)
        live_ingress.close("validation-replay")
        pipeline.aux_tasks.append(
            asyncio.create_task(
                _live_gps(live_ingress, collector_stop, spec.live_gps_hz, artifacts),
                name="c10-live-gps",
            )
        )
    return collector_task


async def _arm(
    spec: ScenarioSpec,
    *,
    label: str,
    config: CanaryConfig,
    profile: CaptureProfile,
    pipeline: _Pipeline,
    artifacts: RunArtifacts,
) -> asyncio.Task[None]:
    """The capture boundary, exactly as the control route draws it; returns
    the scenario timeline's task, started at the arm instant."""
    health = pipeline.components.health_monitor
    writer, async_writer = pipeline.writer, pipeline.async_writer
    start_epoch: StartEpoch | None = None
    capture_dir: Path | None = None
    if spec.kind != "replay":
        # Production writes a manifest (with the C09 plan) only for captures;
        # ``default`` is normal collection and gets a plan-less sidecar here
        # purely so the recompute path can judge it. An AUTO plan would list
        # the whole decodable universe and judge every unsupported PID as an
        # unanswered input, which is not what the daemon does.
        plan = resolved_plan_for(config, profile) if profile.name != "default" else None
        artifacts.resolved_plan_written = plan is not None
        if profile.name != "default":
            capture_id = f"c10-{label}-{int(time.time())}"
            accepts_plan = (
                "resolved_polling_plan"
                in inspect.signature(ParquetTelemetryWriter.enter_capture_mode).parameters
            )
            if accepts_plan:
                await async_writer.enter_capture_mode(profile, capture_id, None, plan)
            else:
                await async_writer.enter_capture_mode(profile, capture_id)
            arm_profile(profile, session_dir=writer.paths.root)
        else:
            write_default_manifest(writer.paths.root, profile, plan=plan)
        capture_dir = writer.paths.root
        start_epoch = record_capture_start(capture_dir, health)
    artifacts.capture_dir = capture_dir
    artifacts.start_epoch = start_epoch
    artifacts.armed_wall = time.time()
    artifacts.armed_monotonic = time.monotonic()
    armed_loop = asyncio.get_running_loop().time()
    return asyncio.create_task(_timeline(spec.timeline, armed_loop, artifacts, writer))


async def _finish(
    *,
    pipeline: _Pipeline,
    artifacts: RunArtifacts,
    collector_task: asyncio.Task[Any],
    timeline_task: asyncio.Task[None],
    rusage_before: dict[str, float],
    default_profile: CaptureProfile,
) -> None:
    """Stop — boundary, drain, flush, judge (C03.8) while the collector runs —
    then tear down in dependency order and fill in the artifacts."""
    components = pipeline.components
    health = components.health_monitor
    writer = pipeline.writer
    artifacts.scheduler_metrics_before_stop = artifacts.scheduler_metrics
    if artifacts.capture_dir is not None:
        artifacts.finalize = await finalize_capture(
            artifacts.capture_dir,
            health=health,
            writer=pipeline.async_writer,
            start=artifacts.start_epoch,
        )
    artifacts.stop_epoch = health.loss_epoch().to_dict()
    pipeline.collector_stop.set()
    await timeline_task
    await collector_task
    pipeline.inference_stop.set()
    if pipeline.inference_task is not None:
        await pipeline.inference_task
    pipeline.drain_stop.set()
    await pipeline.drain_task
    pipeline.aux_stop.set()
    await asyncio.gather(*pipeline.aux_tasks)
    components.telemetry_broadcaster.unsubscribe(pipeline.telemetry_sub)
    components.scores_broadcaster.unsubscribe(pipeline.scores_sub)
    artifacts.ended_wall = time.time()
    rusage_after = _rusage()

    artifacts.session_dirs = [paths.root for paths in writer.session_paths]
    artifacts.rows_written = writer.rows_written
    artifacts.writer_flush = writer.flush_samples.summary_ms()
    artifacts.writer_stalls = writer.stalls
    artifacts.can_frames_suppressed = writer.can_frames_suppressed_total
    artifacts.heartbeat = pipeline.heartbeat.summary_ms()
    artifacts.wall_clock_steps = [
        {**step, "at_s_after_arm": round(step["at_monotonic"] - artifacts.armed_monotonic, 3)}
        for step in pipeline.clock_steps
    ]
    artifacts.rusage = _rusage_delta(
        rusage_before, rusage_after, artifacts.ended_wall - artifacts.armed_wall
    )
    artifacts.dashboard = {
        "received": pipeline.dashboard_count[0],
        "dropped": pipeline.telemetry_sub.dropped,
    }
    artifacts.scores = dict(pipeline.scores)
    artifacts.health_snapshot = _health_snapshot(health)
    artifacts.inference = (
        components.inference_timing.snapshot().to_dict() if pipeline.live_source else None
    )
    artifacts.emitter_counters = _counters_dict(components.emitter.counters)
    stub = components.model_managers.get("isolation_forest")
    if isinstance(stub, StubModelManager):
        artifacts.if_stub_calls = stub.artifact.calls
    reset_process_state(default_profile)
    _unsubscribe_all(components.telemetry_broadcaster)


async def run_scenario(
    spec: ScenarioSpec,
    *,
    label: str,
    sessions_root: Path,
    scratch: Path,
    config: CanaryConfig | None = None,
    enable_inference: bool = True,
) -> RunArtifacts:
    """Run ``spec`` end to end and return the raw artifacts (see module doc).

    Start the pipeline, start the collector, warm up, arm, run, finish
    (R04.13: the four phases were one 324-line function).
    """
    scratch.mkdir(parents=True, exist_ok=True)
    config = config if config is not None else harness_config(scratch)
    default_profile, _ = build_profile(
        {"name": "default", "mode01": {"AUTO": 1.0}, "mode22": ALL_DIDS, "corpus_eligible": True}
    )
    reset_process_state(default_profile)
    profile, profile_dropped = build_profile(spec.profile)

    components = build_components(config)
    models = install_models(components, config) if enable_inference else {"status": "not_built"}

    synthetic = spec.kind != "replay"
    bindings_path = (
        write_synthetic_bindings(scratch / "synthetic_bindings.yaml")
        if synthetic
        else REAL_BINDINGS
    )
    pipeline = _start_pipeline(
        spec,
        label=label,
        sessions_root=sessions_root,
        components=components,
        config=config,
        bindings_path=bindings_path,
        enable_inference=enable_inference,
    )
    can_spec: CanObdSpec | None = None
    spec_dropped: list[str] = []
    if spec.kind == "canable":
        can_spec, spec_dropped = build_can_spec(config, spec)

    live = pipeline.live_source is not None
    artifacts = RunArtifacts(
        scenario=spec,
        label=label,
        config=config,
        session_dirs=[],
        capture_dir=None,
        profile=profile,
        profile_fields_dropped=profile_dropped,
        spec_fields_dropped=spec_dropped,
        can_spec=can_spec,
        bindings_path=bindings_path,
        models=models,
        components_enabled={
            "collector": spec.kind,
            # P02.8: decoded rows are never stored; the storage sink writes
            # the raw frames and the readers decode them on read.
            "decode_storage": False,
            "dashboard_broadcast": True,
            "live_feature_source": live,
            "inference_loop": live,
            "derived_channels": live,
            "dtc_sink": True,
            "monitor_sink": True,
            "gps": (
                f"simulated live receiver at {spec.live_gps_hz:g} Hz on the replay queue"
                if spec.live_gps_hz > 0
                else False
            ),
            "elm327": "not_exercised: needs the ELM327-emulator serial integration tier",
            "hardware_adapter": "synthetic",
            "kernel_counters": "unavailable (no can0 on this host)",
        },
    )

    collector_task = await _start_collector(
        spec,
        label=label,
        config=config,
        can_spec=can_spec,
        pipeline=pipeline,
        artifacts=artifacts,
    )
    await asyncio.sleep(spec.warmup_s)
    timeline_task = await _arm(
        spec, label=label, config=config, profile=profile, pipeline=pipeline, artifacts=artifacts
    )
    rusage_before = _rusage()
    await asyncio.sleep(spec.duration_s)
    await _finish(
        pipeline=pipeline,
        artifacts=artifacts,
        collector_task=collector_task,
        timeline_task=timeline_task,
        rusage_before=rusage_before,
        default_profile=default_profile,
    )
    return artifacts


async def run_scenario_clean(
    spec: ScenarioSpec,
    *,
    label: str,
    sessions_root: Path,
    scratch: Path,
    config: CanaryConfig | None = None,
    enable_inference: bool = True,
    attempts: int = 3,
) -> RunArtifacts:
    """:func:`run_scenario`, rerun (bounded) when the host wall clock stepped
    during the run. A step re-orders wall-stamped rows across it, so every
    pairing and cadence figure from such a run is an artefact of the host,
    not of the daemon; the last attempt is returned either way and says so."""
    artifacts: RunArtifacts | None = None
    for attempt in range(1, max(1, attempts) + 1):
        artifacts = await run_scenario(
            spec,
            label=label,
            sessions_root=sessions_root,
            scratch=scratch,
            config=config,
            enable_inference=enable_inference,
        )
        artifacts.attempts_used = attempt
        if not artifacts.clock_stepped:
            break
        log.warning(
            "scenario %s attempt %d saw a host wall-clock step (%s); rerunning",
            spec.name,
            attempt,
            artifacts.wall_clock_steps,
        )
    assert artifacts is not None
    return artifacts


def _health_snapshot(health: Any) -> dict[str, Any]:
    try:
        snapshot = health.snapshot()
    except Exception as exc:  # noqa: BLE001 — the snapshot is evidence, not the run
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "telemetry_sinks": snapshot.get("telemetry_sinks"),
        "system": snapshot.get("system"),
        "capture_lifetime": snapshot.get("capture_lifetime"),
    }


def _unsubscribe_all(broadcaster: Broadcaster) -> None:
    for sub in list(broadcaster._subscribers):  # noqa: SLF001 — harness teardown
        broadcaster.unsubscribe(sub)


__all__ = [
    "DRIVE_SLICE",
    "LIVE_GPS_SENTINEL",
    "REAL_BINDINGS",
    "BoundedSamples",
    "InstrumentedParquetWriter",
    "RunArtifacts",
    "StubIsolationForest",
    "arm_profile",
    "build_can_spec",
    "harness_config",
    "install_models",
    "reset_process_state",
    "resolved_plan_for",
    "run_scenario",
    "run_scenario_clean",
]
