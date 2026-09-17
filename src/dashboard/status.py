"""Dashboard status snapshot assembly."""

from __future__ import annotations

import json
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from src.alerts.emitter import AnomalyEventEmitter
from src.analysis.gps_tracker import GpsTracker
from src.collection import bus_budget as bus_budget_gate
from src.collection import tuning_profile, tx_gate
from src.config import CanaryConfig
from src.control.replay_control import ReplayController
from src.models.feature_spec_health import structural_blindness_snapshot
from src.models.health import HealthMonitor
from src.timefmt import parse_iso_utc

if TYPE_CHECKING:  # annotation only — src.daemon's package __init__ imports the dashboard
    from src.daemon.inference_metrics import InferenceTimingRecorder

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
    #: Wall clock the freshness numbers above were measured against, stamped
    #: by :func:`_derive_vehicle_state`. Deriving this state reads Parquet, so
    #: it is the one part of ``/status`` that gets reused across requests
    #: (:class:`VehicleStateCache`) — carrying the derivation instant *inside*
    #: the state means no consumer can mistake a reused reading for a fresh
    #: one, whichever view or endpoint handed it over. ``None`` only for a
    #: state built by calling :func:`classify_vehicle_state` directly.
    derived_at: str | None = None

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
            "derived_at": self.derived_at,
        }


def classify_vehicle_state(
    *,
    rpm: float | None,
    coolant_temp: float | None,
    run_time: float | None,
    obd_freshness_seconds: float | None,
    can_freshness_seconds: float | None,
    replay_id: str | None = None,
    rpm_from_broadcast: bool = False,
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

    ``rpm_from_broadcast`` says the ``rpm`` argument came from the decoded
    broadcast bus rather than an OBD reply, which makes rule 5's inference
    unsafe: during an all-DID capture the ECM stops answering Mode 01 **by
    design** (session 0xC0 and Mode 01 are mutually exclusive on this ECM),
    so "OBD stale" no longer implies a powered-down ECM. A fresh broadcast
    RPM above the threshold is direct evidence the engine is turning, so it
    outranks that inference. Only set it when the value really is broadcast-
    sourced — an OBD-sourced rpm is as stale as its OBD read.
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

    engine_evidence_fresh = obd_fresh or (can_fresh and rpm_from_broadcast)

    if not obd_fresh and not can_fresh:
        state: VehicleStateName = "no_data"
    elif engine_evidence_fresh and rpm is not None and rpm >= rpm_running_threshold:
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
            "passive": self.passive,
            "tuning_mode": self.tuning_mode,
            "bus_budget": self.bus_budget,
            "capture_profile": self.capture_profile,
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
    """The session being written right now — captures included.

    Previously this reused ``discover_sessions``, which excludes
    ``_``-prefixed directories so captures stay out of the training corpus.
    Correct for training, wrong here: every live read inherited the exclusion
    and silently showed the *previous* session for the whole duration of a
    capture. Fixing it per-caller would have needed six call sites to
    remember; fixing the resolver makes them right by construction.
    """
    from src.control.sessions import latest_session

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


#: `decoded:ECM_RPM` is stored raw (8 counts/rpm) so training's scaler stays
#: comparable across sessions; see src/decode/frame_decoder.py. Taking it at
#: face value reads as a factor-of-8 bug.
_ECM_RPM_SCALE = 0.125


def _derive_vehicle_state(
    storage_path: Path,
    *,
    replay_controller: ReplayController | None,
    now: datetime | None = None,
    session_dir: Path | None = None,
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
    decoded_rpm: float | None = None
    coolant: float | None = None
    run_time: float | None = None
    obd_latest_ts: datetime | None = None
    can_latest_ts: datetime | None = None

    # Deferred import: src.dashboard.readers imports _resolve_latest_session
    # from this module, so importing it at module load creates a cycle.
    from src.dashboard.readers import read_latest_per_channel

    try:
        rows = read_latest_per_channel(storage_path, session_dir=session_dir)
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
        elif source == "decoded":
            # Broadcast decode never stops, even when Mode 01 is dark — which
            # it is *by ECU design* during an all-DID capture, since session
            # 0xC0 blocks Mode 01. Treat it as CAN liveness, and use its RPM
            # when the OBD one is absent.
            if ts is not None and (can_latest_ts is None or ts > can_latest_ts):
                can_latest_ts = ts
            # Captured unconditionally: whether it is *preferred* depends on how
            # stale the OBD rpm turns out to be, and freshness is not known
            # until the loop has finished.
            if channel == "ECM_RPM" and isinstance(value, (int, float)):
                decoded_rpm = float(value)

    obd_freshness = (now_dt - obd_latest_ts).total_seconds() if obd_latest_ts is not None else None
    can_freshness = (now_dt - can_latest_ts).total_seconds() if can_latest_ts is not None else None

    rpm_from_broadcast = False
    obd_rpm_usable = rpm is not None and (
        obd_freshness is not None and obd_freshness <= _FRESHNESS_THRESHOLD_SECONDS
    )
    if not obd_rpm_usable and decoded_rpm is not None:
        # A *stale* OBD rpm is no more informative than a missing one, and it
        # is worse: it looks like data. This condition used to be `rpm is
        # None`, so a held-over reading kept the broadcast fallback from ever
        # firing. Observed 2026-08-07 with a second scan tool on an OBD-II
        # Y-cable: it took the ECM's tester session, canary's Mode 01 went
        # unanswered, and a 27 s stale rpm of 750 held while the broadcast
        # reported 781 rpm at 96 Hz. The state read `engine_off` — "plugged in,
        # key out" — with the engine idling.
        #
        # `decoded:` rows carry raw counts by design; ECM_RPM is 8 per rpm.
        rpm = decoded_rpm * _ECM_RPM_SCALE
        rpm_from_broadcast = True

    # `derived_at` is stamped here, not in the pure classifier: it is a fact
    # about the *read*, and every path that reuses a state (the /status views
    # share one through `VehicleStateCache`) needs the age to travel with it.
    return replace(
        classify_vehicle_state(
            rpm=rpm,
            rpm_from_broadcast=rpm_from_broadcast,
            coolant_temp=coolant,
            run_time=run_time,
            obd_freshness_seconds=obd_freshness,
            can_freshness_seconds=can_freshness,
            replay_id=replay_id,
        ),
        derived_at=now_dt.isoformat(),
    )


def _active_session_dir(storage_writer: Any) -> Path | None:
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


#: How long one derived :class:`VehicleState` may be reused across ``/status``
#: requests. Deriving it reads the newest session's Parquet chunks and is
#: essentially the entire cost of the payload — measured 2026-09-17 on a
#: 486-chunk session: 390 ms of a 400 ms build on the dev laptop, and the Pi
#: served the full payload in 1.3–2.6 s. Deliberately far below the
#: classifier's own ``_FRESHNESS_THRESHOLD_SECONDS`` (10 s), so reuse cannot
#: change a verdict that a single slow request would not have changed anyway,
#: and ``VehicleState.derived_at`` reports the age regardless.
VEHICLE_STATE_TTL_SECONDS = 2.0


class VehicleStateCache:
    """One derived vehicle state, shared by every ``/status`` view.

    Owned by the route registration, so no caller has to remember to pass it
    and both views necessarily read the same object: the compact poller and
    the full payload cannot report different vehicle states. Repeat polls pay
    one Parquet read per :data:`VEHICLE_STATE_TTL_SECONDS` instead of one per
    request, which is what makes a 1 Hz watchdog poll affordable on the Pi.

    Not used by the replay guard (``POST /control/replay``), which is a
    one-shot decision rather than a poll and reads fresh.
    """

    def __init__(self, ttl_seconds: float = VEHICLE_STATE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._entry: tuple[float, VehicleState] | None = None
        self._lock = threading.Lock()

    def get(self, derive: Callable[[], VehicleState]) -> VehicleState:
        """The cached state, or ``derive()`` when it has aged past the TTL."""
        entry = self._entry
        if entry is not None and time.monotonic() - entry[0] < self._ttl:
            return entry[1]
        state = derive()
        with self._lock:
            self._entry = (time.monotonic(), state)
        return state


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


def gather_status_sources(
    config: CanaryConfig,
    *,
    emitter: AnomalyEventEmitter | None = None,
    health_monitor: HealthMonitor | None = None,
    replay_controller: ReplayController | None = None,
    storage_writer: Any = None,
    inference_timing: InferenceTimingRecorder | None = None,
    vehicle_state_cache: VehicleStateCache | None = None,
) -> StatusSources:
    """Read every source both ``/status`` views need, exactly once.

    ``vehicle_state_cache`` shares the Parquet-backed derivation across
    requests; without one, every call derives fresh (what a test or a
    standalone :func:`build_status` caller wants).
    """
    storage = config.collection.storage_path

    def derive() -> VehicleState:
        return _derive_vehicle_state(
            storage,
            replay_controller=replay_controller,
            session_dir=_active_session_dir(storage_writer),
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
        }
    if sources.inference is not None:
        detectors = {**(detectors or {}), "inference": sources.inference}
    return StatusSnapshot(
        storage_path=str(storage),
        free_mb=sources.free_mb,
        latest_session=latest.name if latest else None,
        latest_session_path=str(latest) if latest else None,
        model_versions=model_versions,
        vehicle=f"{config.vehicle.year} {config.vehicle.make} {config.vehicle.model}".strip(),
        emitter_counters=sources.emitter_counters,
        health=sources.health,
        vehicle_state=sources.vehicle_state,
        gps=gps_tracker.gps_status() if gps_tracker is not None else None,
        trip=gps_tracker.trip_status() if gps_tracker is not None else None,
        profile=_active_profile(config),
        detectors=detectors,
        model_provenance=model_provenance if models_dir is not None else None,
        passive=sources.passive,
        tuning_mode=tuning_profile.snapshot(),
        bus_budget=sources.bus_budget,
        capture_profile=sources.capture_profile,
    )


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
